'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { spawn, execFile } = require('node:child_process');
const { promisify } = require('node:util');
const execute = promisify(execFile);
const identity = require('./identity.cjs');

// Upstream sidecars own authentication, forwarding, and MCP enforcement.
// https://github.com/github/gh-aw-firewall/tree/v0.28.25/containers/api-proxy
// https://github.com/github/gh-aw-mcpg/blob/v0.4.25/docs/CONFIGURATION.md
const MODEL_IMAGE = 'ghcr.io/github/gh-aw-firewall/api-proxy:0.28.25@sha256:c3c7082e73ba83052c580097e5a9c9c40a11e6f6b6fa0a4710e6859f83a62854';
const GATEWAY_IMAGE = 'ghcr.io/github/gh-aw-mcpg:v0.4.25@sha256:9be0a86220e807a0ecc89e53d7453468f7a53fbc6b3d1efd2299025ffe01d086';
const BUN_IMAGE = 'oven/bun:1.3.14-slim@sha256:d56a2534ffd262e92c12fd3249d3924d296d97086da773f821d7d0477435ea04';
const GITHUB_IMAGE = 'ghcr.io/github/github-mcp-server:v1.12.2@sha256:508a0857ec762b1ab1cece29193345b501fab1dd9d1228a7b617062954cecac6';
const GITHUB_TOOLS = Object.freeze(['get_file_contents', 'get_commit', 'list_commits', 'search_code', 'pull_request_read', 'issue_read', 'add_issue_comment']);
const INLINE_TOOLS = Object.freeze(['create_inline_comment']);

function gatewayConfig({ id, actionDir, inline, githubImage, bunImage }) {
  return {
    gateway: {
      port: 8080, domain: 'mcpg', agentId: id, startupTimeout: 60,
      payloadDir: '/review-payloads', payloadPathPrefix: '/review-payloads',
      agentPolicies: { [id]: { servers: ['github', 'github_inline_comment'], enclave: true } },
    },
    mcpServers: {
      github: {
        type: 'stdio', container: githubImage,
        args: ['--pull', 'never', '--label', `gumnut.claude-review-session=${id}`],
        entrypointArgs: ['stdio', '--toolsets=', `--tools=${GITHUB_TOOLS.join(',')}`],
        env: { GITHUB_PERSONAL_ACCESS_TOKEN: '', GITHUB_HOST: '' }, tools: [...GITHUB_TOOLS],
      },
      github_inline_comment: {
        type: 'stdio', container: bunImage,
        args: ['--pull', 'never', '--label', `gumnut.claude-review-session=${id}`], entrypoint: 'bun', entrypointArgs: inline.args,
        mounts: [`${actionDir}:${actionDir}:ro`],
        env: { ...inline.env, GITHUB_TOKEN: '' }, tools: [...INLINE_TOOLS],
      },
    },
  };
}

async function docker(args, { env = {}, timeout = 15000 } = {}) {
  try {
    return await execute('docker', args, {
      env: { PATH: process.env.PATH, ...env },
      timeout, maxBuffer: 1024 * 1024,
    });
  } catch { throw new Error('Review service Docker command failed'); }
}

async function prepareImages(dependencies = {}) {
  const run = dependencies.docker || docker;
  for (const image of [MODEL_IMAGE, GATEWAY_IMAGE, GITHUB_IMAGE, BUN_IMAGE]) {
    await run(['pull', image], { timeout: 120000 });
  }
}

function attachGateway(name, config, env) {
  const child = spawn('docker', ['start', '--attach', '--interactive', name], {
    env: { PATH: process.env.PATH, ...env }, stdio: ['pipe', 'ignore', 'ignore'],
  });
  child.stdin.on('error', () => {});
  child.once('error', () => {});
  child.stdin.end(JSON.stringify(config));
  return child;
}

async function startServices({ sessionDir, env, mcpConfig }, dependencies = {}) {
  const command = dependencies.docker || docker;
  let interrupted = false;
  let starting = true;
  const run = (args, options) => {
    if (interrupted) return Promise.reject(new Error('Review services interrupted'));
    return command(args, options);
  };
  const attach = dependencies.attachGateway || attachGateway;
  const pause = dependencies.pause || (ms => new Promise(resolve => setTimeout(resolve, ms)));
  const id = dependencies.id || randomUUID();
  const prefix = `claude-review-${id}`;
  const network = `${prefix}-agent`;
  const names = { model: `${prefix}-model`, gateway: `${prefix}-mcpg` };
  const actionDir = await fs.realpath(env.GITHUB_ACTION_PATH || '');
  const suppliedInline = mcpConfig?.mcpServers?.github_inline_comment;
  const owner = env.CLAUDE_REVIEW_OWNER;
  const repo = env.CLAUDE_REVIEW_REPO;
  const number = env.CLAUDE_REVIEW_PR_NUMBER;
  if (!/^[A-Za-z0-9-]+$/.test(owner || '') || !/^[A-Za-z0-9_.-]+$/.test(repo || '') || !/^[1-9][0-9]*$/.test(number || '')) throw new Error('Prepared review identity unavailable');
  const inline = suppliedInline && { ...suppliedInline, env: {
    REPO_OWNER: owner, REPO_NAME: repo, PR_NUMBER: number, CLASSIFY_INLINE_COMMENTS: 'false',
  } };
  if (!env.GITHUB_TOKEN || !env.CLAUDE_CODE_OAUTH_TOKEN) throw new Error('Review service credentials unavailable');
  if (!inline || inline.command !== 'bun' || !Array.isArray(inline.args) || inline.args.some(value => typeof value !== 'string')) throw new Error('Trusted inline backend configuration unavailable');
  // MCpg's pinned schema accepts Docker image IDs, but not registry digest
  // references. Resolve only the already-pulled digest pins; never fall back to
  // a mutable tag or let backend startup pull a different image.
  const backendImages = [];
  for (const image of [GITHUB_IMAGE, BUN_IMAGE]) {
    const result = await run(['image', 'inspect', '--format', '{{.Id}}', image]);
    const imageID = (result.stdout || '').trim();
    if (!/^sha256:[a-f0-9]{64}$/.test(imageID)) throw new Error('Pinned backend image unavailable');
    backendImages.push(imageID);
  }
  const payloadDir = path.join(sessionDir, 'payloads');
  await fs.mkdir(payloadDir, { recursive: true, mode: 0o755 });
  const socket = await (dependencies.socketStat || fs.stat)('/var/run/docker.sock');
  if (!Number.isInteger(socket.gid) || socket.gid < 0) throw new Error('Docker socket group unavailable');
  const config = gatewayConfig({ id, actionDir, inline, githubImage: backendImages[0], bunImage: backendImages[1] });
  let child;
  let closed;
  const close = () => {
    if (closed) return closed;
    closed = (async () => {
      process.removeListener('SIGTERM', signalCleanup);
      process.removeListener('SIGINT', signalCleanup);
      // Docker may create a resource before a timeout loses its acknowledgement.
      // Sidecars have unique names; MCpg owns its per-session backend names.
      for (const name of [names.gateway, names.model]) await command(['rm', '--force', name]).catch(() => {});
      child?.kill('SIGTERM');
      const listed = await command(['ps', '--all', '--quiet', '--no-trunc', '--filter', `label=gumnut.claude-review-session=${id}`]).catch(() => ({ stdout: '' }));
      const backendIDs = new Set((listed.stdout || '').trim().split(/\s+/).filter(value => /^[a-f0-9]{64}$/.test(value)));
      for (const backendID of backendIDs) await command(['rm', '--force', backendID]).catch(() => {});
      await command(['network', 'rm', network]).catch(() => {});
    })();
    return closed;
  };
  const signalCleanup = () => { interrupted = true; if (!starting) void close(); };
  process.on('SIGTERM', signalCleanup);
  process.on('SIGINT', signalCleanup);
  try {
    await run(['network', 'create', '--internal', network]);
    const modelEnv = {
      ANTHROPIC_API_KEY: `Bearer ${env.CLAUDE_CODE_OAUTH_TOKEN}`,
      AWF_ANTHROPIC_AUTH_HEADER: 'Authorization', AWF_VALIDATE_KEYS: 'off',
      AWF_MODEL_FALLBACK: '{"enabled":false}',
      AWF_ANTHROPIC_AUTO_CACHE: 'false', AWF_ANTHROPIC_STRIP_ANSI: 'false',
    };
    await run(['create', '--name', names.model, '--network', 'bridge', '--log-driver', 'none',
      ...Object.keys(modelEnv).flatMap(key => ['--env', key]), MODEL_IMAGE], { env: modelEnv });
    await run(['network', 'connect', '--alias', 'model-proxy', network, names.model]);
    await run(['start', names.model]);
    const gatewayEnv = {
      HOME: '/tmp/review-mcpg', DOCKER_HOST: 'unix:///var/run/docker.sock',
      MCP_GATEWAY_PORT: '8080', MCP_GATEWAY_DOMAIN: 'mcpg', MCP_GATEWAY_AGENT_ID: id,
      MCP_GATEWAY_ALLOWED_MOUNT_ROOTS: `${actionDir}:ro`,
      GITHUB_ACTION_PATH: actionDir,
      GITHUB_TOKEN: env.GITHUB_TOKEN, GITHUB_PERSONAL_ACCESS_TOKEN: env.GITHUB_TOKEN,
      GITHUB_HOST: env.GITHUB_SERVER_URL || 'https://github.com',
      MCP_GATEWAY_UNSAFE_RAW_ENCLAVE_PAYLOAD_LOGS: 'false',
      MCP_GATEWAY_WASM_GUARDS_DIR: '', DEBUG: '',
    };
    // The image wrapper requires host-published ports. The documented binary
    // entrypoint keeps this service reachable only through the agent network.
    await run(['create', '--interactive', '--name', names.gateway, '--network', 'bridge', '--log-driver', 'none',
      '--user', identity.user, '--group-add', String(socket.gid),
      '--entrypoint', '/app/awmg',
      '--volume', '/var/run/docker.sock:/var/run/docker.sock',
      '--volume', `${actionDir}:${actionDir}:ro`, '--volume', `${payloadDir}:/review-payloads:rw`,
      ...Object.keys(gatewayEnv).flatMap(key => ['--env', key]), GATEWAY_IMAGE,
      '--listen', '0.0.0.0:8080', '--routed', '--config-stdin', '--log-dir', '/tmp/review-mcpg/logs'], { env: gatewayEnv });
    await run(['network', 'connect', '--alias', 'mcpg', network, names.gateway]);
    child = attach(names.gateway, config, gatewayEnv);
    const deadline = Date.now() + (dependencies.readinessTimeoutMs || 90000);
    const readiness = 'Promise.all(["http://localhost:10000/health","http://mcpg:8080/health"].map(async u=>{const r=await fetch(u,{signal:AbortSignal.timeout(2000)});if(!r.ok||(await r.json()).status!=="healthy")throw Error("unready")})).catch(()=>process.exit(1))';
    for (;;) {
      try { await run(['exec', names.model, 'node', '-e', readiness]); break; }
      catch {
        if (interrupted) throw new Error('Review services interrupted');
        if (closed || Date.now() >= deadline) throw new Error('Review services failed readiness');
        await pause(500);
      }
    }
    if (closed || interrupted) throw new Error('Review services interrupted');
    starting = false;
    return {
      network, modelURL: 'http://model-proxy:10001', payloadDir,
      mcpConfig: { mcpServers: Object.fromEntries(['github', 'github_inline_comment'].map(server => [server, {
        type: 'http', url: `http://mcpg:8080/mcp/${server}`, headers: { Authorization: id },
      }])) }, close,
    };
  } catch (error) { await close(); throw error; }
}

module.exports = { prepareImages, startServices, gatewayConfig, MODEL_IMAGE, GATEWAY_IMAGE, GITHUB_IMAGE, BUN_IMAGE, GITHUB_TOOLS, INLINE_TOOLS };
