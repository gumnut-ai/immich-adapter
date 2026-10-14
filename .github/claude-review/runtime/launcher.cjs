#!/usr/bin/env node
'use strict';

// The official action and SDK stay on the host. This executable replaces only
// the SDK's CLI subprocess, preserving its stream-json stdin/stdout protocol.
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { spawn, execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { pipeline } = require('node:stream/promises');
const { randomUUID } = require('node:crypto');
const exec = promisify(execFile);
const identity = require('./identity.cjs');

const DUMMY_OAUTH = 'isolated-review-public-placeholder';
const VALUE_FLAGS = new Set([
  '--output-format', '--input-format', '--model', '--fallback-model', '--effort',
  '--max-turns', '--max-budget-usd', '--max-thinking-tokens', '--thinking',
  '--thinking-display', '--permission-mode', '--permission-prompt-tool',
  '--allowedTools', '--allowed-tools', '--disallowedTools', '--disallowed-tools',
  '--tools', '--agents', '--agent', '--append-system-prompt', '--system-prompt',
  '--json-schema', '--betas', '--session-id', '--permission-prompts',
]);
const SWITCH_FLAGS = new Set([
  '--verbose', '--print', '-p', '--include-partial-messages', '--include-hook-events',
  '--no-session-persistence', '--restricted', '--strict-mcp-config',
]);
const SOURCE_FLAGS = new Set(['--settings']);
const DROP_VALUE_FLAGS = new Set(['--mcp-config', '--setting-sources', '--debug-file']);

function relativeSource(value, repository) {
  const absolute = path.resolve(repository, value);
  const relative = path.relative(repository, absolute);
  if (!relative || relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) throw new Error('Unsupported source configuration');
  return relative.split(path.sep).join('/');
}

function sanitizeArgs({ argv, repository, mcpConfig, secrets = [] }) {
  const clean = [];
  for (let index = 0; index < argv.length; index++) {
    const argument = argv[index];
    const separator = argument.indexOf('=');
    const flag = separator === -1 ? argument : argument.slice(0, separator);
    const inline = separator === -1 ? undefined : argument.slice(separator + 1);
    if (SWITCH_FLAGS.has(flag)) {
      if (inline !== undefined) throw new Error('Unsupported CLI argument');
      if (flag !== '--strict-mcp-config') clean.push(flag);
      continue;
    }
    if (!VALUE_FLAGS.has(flag) && !SOURCE_FLAGS.has(flag) && !DROP_VALUE_FLAGS.has(flag)) throw new Error('Unsupported CLI argument');
    const value = inline === undefined ? argv[++index] : inline;
    if (value === undefined) throw new Error('Missing CLI argument value');
    if (DROP_VALUE_FLAGS.has(flag)) continue;
    if (secrets.some(secret => secret && value.includes(secret))) throw new Error('Credential found in CLI arguments');
    if (SOURCE_FLAGS.has(flag)) { clean.push(flag, relativeSource(value, repository)); continue; }
    if (flag === '--agents' && !value.trim().startsWith('{')) { clean.push(flag, relativeSource(value, repository)); continue; }
    clean.push(flag, value);
  }
  // No project/user MCP discovery or settings may add a credentialed server.
  const config = JSON.stringify(mcpConfig);
  if (secrets.some(secret => secret && config.includes(secret))) throw new Error('Credential found in agent MCP configuration');
  clean.push('--setting-sources', '', '--strict-mcp-config', '--mcp-config', config,
    '--add-dir', '/scratch', '/review-payloads');
  return clean;
}

function agentEnv({ modelURL }) {
  const url = new URL(modelURL);
  if (url.origin !== 'http://model-proxy:10001' || url.pathname !== '/' || url.search || url.hash || url.username || url.password) throw new Error('Unsupported model proxy');
  return {
    HOME: '/scratch/home', CLAUDE_CONFIG_DIR: '/scratch/config', TMPDIR: '/scratch/tmp',
    XDG_CONFIG_HOME: '/scratch/home/.config', XDG_CACHE_HOME: '/scratch/home/.cache',
    CLAUDE_CODE_OAUTH_TOKEN: DUMMY_OAUTH, ANTHROPIC_BASE_URL: url.origin,
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1', CLAUDE_CODE_ENTRYPOINT: 'sdk-ts',
    CI: 'true', TERM: 'dumb', LANG: 'C.UTF-8',
  };
}

function dockerArgs({ image, network, sourceDir, payloadDir, name, argv, modelURL }) {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(network) || !/^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(name) || !path.isAbsolute(sourceDir) || /[,\r\n\0]/.test(sourceDir)) throw new Error('Invalid runtime configuration');
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_./:@-]*$/.test(image)) throw new Error('Invalid runtime image');
  if (payloadDir && (!path.isAbsolute(payloadDir) || /[,\r\n\0]/.test(payloadDir))) throw new Error('Invalid runtime payload path');
  const env = agentEnv({ modelURL });
  return [
    'run', '--rm', '-i', '--name', name, '--network', network,
    '--user', identity.user, '--cap-drop=ALL', '--security-opt=no-new-privileges',
    '--read-only', '--pids-limit=256', '--memory=4g', '--cpus=2',
    '--mount', `type=bind,source=${sourceDir},target=/workspace,readonly`,
    ...(payloadDir ? ['--mount', `type=bind,source=${payloadDir},target=/review-payloads,readonly`] : []),
    '--tmpfs', `/scratch:rw,nosuid,nodev,uid=${identity.uid},gid=${identity.gid},mode=700,size=1g`,
    '--workdir', '/workspace',
    ...Object.entries(env).flatMap(([key, value]) => ['--env', `${key}=${value}`]),
    image, ...argv,
  ];
}

async function archiveSource({ repository, destination }) {
  await fs.mkdir(destination, { recursive: true, mode: 0o755 });
  const archive = spawn('git', ['-C', repository, 'archive', '--format=tar', 'HEAD'], { stdio: ['ignore', 'pipe', 'pipe'] });
  const extract = spawn('tar', ['-xf', '-', '-C', destination], { stdio: ['pipe', 'ignore', 'pipe'] });
  archive.stderr.resume(); extract.stderr.resume();
  const exited = child => new Promise((resolve, reject) => {
    child.once('error', () => reject(new Error('Source archive failed')));
    child.once('close', code => code === 0 ? resolve() : reject(new Error('Source archive failed')));
  });
  try { await Promise.all([pipeline(archive.stdout, extract.stdin), exited(archive), exited(extract)]); }
  catch { archive.kill('SIGTERM'); extract.kill('SIGTERM'); throw new Error('Source archive failed'); }
  // HEAD contains the trusted source snapshot, never .git, untracked files or
  // runner/action directories. Do not bind the host checkout itself.
}

function hostMcpConfig(argv) {
  let servers = {};
  for (let index = 0; index < argv.length; index++) {
    if (argv[index] !== '--mcp-config' && !argv[index].startsWith('--mcp-config=')) continue;
    const value = argv[index] === '--mcp-config' ? argv[++index] : argv[index].slice('--mcp-config='.length);
    let config;
    try { config = JSON.parse(value); } catch { throw new Error('Expected inline trusted MCP configuration'); }
    if (!config || !config.mcpServers || typeof config.mcpServers !== 'object' || Array.isArray(config.mcpServers)) throw new Error('Invalid trusted MCP configuration');
    servers = { ...servers, ...config.mcpServers };
  }
  return { mcpServers: servers };
}

async function runLauncher({ argv = process.argv.slice(2), env = process.env, input = process.stdin, output = process.stdout, startServices, spawnDocker = spawn, archive = archiveSource, dockerExec = exec } = {}) {
  const repository = path.resolve(env.GITHUB_WORKSPACE || process.cwd());
  const sessionDir = await fs.mkdtemp(path.join(os.tmpdir(), 'claude-isolated-'));
  const name = `claude-review-${randomUUID()}`;
  let services;
  let child;
  let signal;
  let cleaning;
  const cleanup = () => cleaning ||= (async () => {
    // Remove only this invocation's uniquely named container; killing the Docker
    // client alone does not guarantee the daemon stops the CLI container.
    if (child) await dockerExec('docker', ['rm', '--force', name], { timeout: 10_000 }).catch(() => {});
    try { if (services) await services.close(); }
    finally { await fs.rm(sessionDir, { recursive: true, force: true }); }
  })();
  const onSignal = value => {
    signal = value;
    child?.kill(value);
    if (child) cleanup().catch(() => {});
  };
  const onTerm = () => onSignal('SIGTERM');
  const onInt = () => onSignal('SIGINT');
  process.on('SIGTERM', onTerm); process.on('SIGINT', onInt);
  try {
    const sourceDir = path.join(sessionDir, 'source');
    await archive({ repository, destination: sourceDir });
    if (signal) throw new Error('Review cancelled');
    const hostConfig = hostMcpConfig(argv);
    services = await (startServices || require('./services.cjs').startServices)({ sessionDir, env, mcpConfig: hostConfig });
    if (signal) throw new Error('Review cancelled');
    const secrets = [env.CLAUDE_CODE_OAUTH_TOKEN, env.GITHUB_TOKEN, env.GH_TOKEN].filter(Boolean);
    const cleanArgv = sanitizeArgs({ argv, repository, mcpConfig: services.mcpConfig, secrets });
    const args = dockerArgs({ image: env.CLAUDE_REVIEW_RUNTIME_IMAGE || 'gumnut-claude-review:2.1.295', network: services.network, sourceDir, payloadDir: services.payloadDir, name, argv: cleanArgv, modelURL: services.modelURL });
    child = spawnDocker('docker', args, { env: { PATH: env.PATH }, stdio: ['pipe', 'pipe', 'pipe'] });
    child.stderr.resume(); // Never echo subprocess errors containing host arguments.
    child.stdin.on('error', () => {});
    input.pipe(child.stdin); child.stdout.pipe(output, { end: false });
    return await new Promise((resolve, reject) => {
      child.once('error', () => reject(new Error('Isolated CLI failed to start')));
      child.once('close', (code, childSignal) => resolve(signal || childSignal ? 130 : code ?? 1));
    });
  } finally {
    process.off('SIGTERM', onTerm); process.off('SIGINT', onInt);
    if (child) { input.unpipe(child.stdin); child.stdout.unpipe(output); }
    await cleanup();
  }
}

if (require.main === module) {
  runLauncher().then(code => { process.exitCode = code; }, () => {
    process.stderr.write('Isolated review runtime failed\n'); process.exitCode = 1;
  });
}
module.exports = { DUMMY_OAUTH, sanitizeArgs, agentEnv, dockerArgs, archiveSource, hostMcpConfig, runLauncher };
