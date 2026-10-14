'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { prepareImages, startServices, GITHUB_TOOLS, GITHUB_IMAGE, BUN_IMAGE, MODEL_IMAGE, GATEWAY_IMAGE } = require('./services.cjs');
const identity = require('./identity.cjs');

async function fixture(t) {
  const sessionDir = await fs.mkdtemp(path.join(os.tmpdir(), 'review-services-'));
  t.after(() => fs.rm(sessionDir, { recursive: true, force: true }));
  const action = path.join(sessionDir, 'action');
  await fs.mkdir(action);
  const calls = [];
  let config;
  const child = { kill: () => calls.push({ args: ['kill-attached'] }) };
  return {
    calls, get config() { return config; },
    options: {
      sessionDir,
      env: { GITHUB_ACTION_PATH: action, CLAUDE_REVIEW_OWNER: 'gumnut-ai', CLAUDE_REVIEW_REPO: 'photos', CLAUDE_REVIEW_PR_NUMBER: '3131', GITHUB_TOKEN: 'fixture-github-secret', CLAUDE_CODE_OAUTH_TOKEN: 'fixture-oauth-secret', UNRELATED_SECRET: 'do-not-copy' },
      mcpConfig: { mcpServers: { github_inline_comment: { command: 'bun', args: ['--no-env-file', '--config=${GITHUB_ACTION_PATH}/bunfig.toml', 'run', '${GITHUB_ACTION_PATH}/src/mcp/github-inline-comment-server.ts'], env: { REPO_OWNER: 'gumnut-ai', REPO_NAME: 'photos', PR_NUMBER: '3131', CLASSIFY_INLINE_COMMENTS: 'false' } } } },
    },
    dependencies: {
      id: 'fixture',
      socketStat: async () => ({ gid: 123 }),
      docker: async (args, options) => { calls.push({ args, options }); return args[0] === 'image' ? { stdout: `sha256:${(args.at(-1) === GITHUB_IMAGE ? 'a' : 'b').repeat(64)}\n` } : {}; },
      attachGateway: (name, suppliedConfig) => { config = suppliedConfig; calls.push({ args: ['attach', name] }); return child; },
    },
  };
}

test('sidecars own credentials; the agent receives only HTTP endpoints and session identity', async t => {
  const f = await fixture(t);
  const services = await startServices(f.options, f.dependencies);
  t.after(services.close);
  assert.equal(services.modelURL, 'http://model-proxy:10001');
  assert.deepEqual(services.mcpConfig.mcpServers.github, { type: 'http', url: 'http://mcpg:8080/mcp/github', headers: { Authorization: 'fixture' } });
  assert.equal(services.payloadDir, path.join(f.options.sessionDir, 'payloads'));
  assert.deepEqual(f.config.mcpServers.github.tools, GITHUB_TOOLS);
  for (const backend of Object.values(f.config.mcpServers)) {
    assert.deepEqual(backend.args, ['--pull', 'never', '--label', 'gumnut.claude-review-session=fixture']);
    assert(!backend.args.includes('--name'));
  }
  assert.deepEqual(f.config.mcpServers.github_inline_comment.tools, ['create_inline_comment']);
  assert.equal(f.config.mcpServers.github.container, `sha256:${'a'.repeat(64)}`);
  assert.equal(f.config.mcpServers.github_inline_comment.container, `sha256:${'b'.repeat(64)}`);
  assert.equal(f.config.gateway.agentPolicies.fixture.enclave, true);
  assert.deepEqual(f.config.mcpServers.github_inline_comment.env, {
    REPO_OWNER: 'gumnut-ai', REPO_NAME: 'photos', PR_NUMBER: '3131', CLASSIFY_INLINE_COMMENTS: 'false', GITHUB_TOKEN: '',
  });
  assert.equal(f.config.gateway.payloadPathPrefix, '/review-payloads');
  const createCalls = f.calls.filter(c => c.args[0] === 'create');
  assert.equal(createCalls.length, 2);
  assert.equal(createCalls[0].args.at(-1), MODEL_IMAGE);
  assert(createCalls[1].args.includes(GATEWAY_IMAGE));
  assert.equal(createCalls[1].args[createCalls[1].args.indexOf('--entrypoint') + 1], '/app/awmg');
  assert(createCalls[1].args.includes('--config-stdin'));
  assert.equal(createCalls[1].args[createCalls[1].args.indexOf('--user')+1], identity.user);
  assert.equal(createCalls[1].args[createCalls[1].args.indexOf('--group-add')+1], '123');
  assert(!createCalls[1].args.includes('--validate-env'));
  assert(!createCalls[1].args.includes('--publish'));
  assert.equal(createCalls[1].options.env.MCP_GATEWAY_WASM_GUARDS_DIR, '');
  assert.equal(createCalls[0].options.env.ANTHROPIC_API_KEY, 'Bearer fixture-oauth-secret');
  assert.equal(createCalls[0].options.env.AWF_ANTHROPIC_AUTH_HEADER, 'Authorization');
  assert.equal(createCalls[1].options.env.GITHUB_TOKEN, 'fixture-github-secret');
  assert.equal(createCalls[1].options.env.GITHUB_ACTION_PATH, await fs.realpath(f.options.env.GITHUB_ACTION_PATH));
  assert.ok(createCalls[1].args.includes('GITHUB_ACTION_PATH'));
  assert.deepEqual(f.config.mcpServers.github_inline_comment.entrypointArgs, [
    '--no-env-file', '--config=${GITHUB_ACTION_PATH}/bunfig.toml', 'run', '${GITHUB_ACTION_PATH}/src/mcp/github-inline-comment-server.ts',
  ]);
  const agentAndArguments = JSON.stringify({ services: { ...services, close: undefined }, args: f.calls.map(c => c.args), config: f.config });
  assert.doesNotMatch(agentAndArguments, /fixture-oauth-secret|fixture-github-secret|do-not-copy/);
  assert.deepEqual(f.calls.filter(c => c.args[0] === 'image').map(c => c.args), [GITHUB_IMAGE,BUN_IMAGE].map(image => ['image','inspect','--format','{{.Id}}',image]));
  assert.deepEqual(f.calls.find(c => c.args[0] === 'network').args, ['network', 'create', '--internal', 'claude-review-fixture-agent']);
  assert.equal(createCalls.filter(c => c.args.includes('/var/run/docker.sock:/var/run/docker.sock')).length, 1);
});

test('close is idempotent and removes sidecars, backend containers and internal network', async t => {
  const f = await fixture(t);
  const before = process.listenerCount('SIGTERM');
  const services = await startServices(f.options, f.dependencies);
  await Promise.all([services.close(), services.close()]);
  const removed = f.calls.filter(c => c.args[0] === 'rm').map(c => c.args.at(-1));
  assert.deepEqual(removed, ['claude-review-fixture-mcpg', 'claude-review-fixture-model']);
  assert.deepEqual(f.calls.at(-1).args, ['network', 'rm', 'claude-review-fixture-agent']);
  assert.equal(process.listenerCount('SIGTERM'), before);
});

test('failed startup removes resources already created without leaking Docker error text', async t => {
  const f = await fixture(t);
  const original = f.dependencies.docker;
  f.dependencies.docker = async (args, options) => {
    if (args[0] === 'start') throw new Error('fixture startup error');
    return original(args, options);
  };
  await assert.rejects(startServices(f.options, f.dependencies), /fixture startup error/);
  assert(f.calls.some(c => c.args[0] === 'rm' && c.args.at(-1) === 'claude-review-fixture-model'));
  assert.deepEqual(f.calls.at(-1).args, ['network', 'rm', 'claude-review-fixture-agent']);
});

test('rejects missing credentials, invalid identity and non-Bun inline entrypoint before Docker', async t => {
  for (const mutation of [f => { delete f.options.env.GITHUB_TOKEN; }, f => { f.options.env.CLAUDE_REVIEW_PR_NUMBER = '3131; injected'; }, f => { f.options.mcpConfig.mcpServers.github_inline_comment.command = 'sh'; }]) {
    const f = await fixture(t); mutation(f);
    await assert.rejects(startServices(f.options, f.dependencies));
    assert.equal(f.calls.length, 0);
  }
});

test('an unavailable digest pin never falls back to a backend tag or starts services', async t => {
  const f = await fixture(t);
  f.dependencies.docker = async args => { f.calls.push({ args }); return { stdout: 'mutable:latest\n' }; };
  await assert.rejects(startServices(f.options, f.dependencies), /Pinned backend image unavailable/);
  assert.deepEqual(f.calls.map(c => c.args[0]), ['image']);
});

test('signal during Docker creation cleans up the newly created resource', async t => {
  const f = await fixture(t);
  const original = f.dependencies.docker;
  f.dependencies.docker = async (args, options) => {
    const result = await original(args, options);
    if (args[0] === 'create') process.emit('SIGTERM');
    return result;
  };
  await assert.rejects(startServices(f.options, f.dependencies), /interrupted/);
  assert(f.calls.some(c => c.args[0] === 'rm' && c.args.at(-1) === 'claude-review-fixture-model'));
  assert.equal(f.calls.filter(c => c.args[0] === 'create').length, 1);
  assert.deepEqual(f.calls.at(-1).args, ['network', 'rm', 'claude-review-fixture-agent']);
});

test('unready services stop at the deadline and remove all resources', async t => {
  const f = await fixture(t);
  const original = f.dependencies.docker;
  f.dependencies.readinessTimeoutMs = 1;
  f.dependencies.pause = async () => {};
  f.dependencies.docker = async (args, options) => {
    if (args[0] === 'exec') throw new Error('unready');
    return original(args, options);
  };
  await assert.rejects(startServices(f.options, f.dependencies), /failed readiness/);
  assert.deepEqual(f.calls.at(-1).args, ['network', 'rm', 'claude-review-fixture-agent']);
});

test('lost create acknowledgements remove every deterministically named resource', async t => {
  for (const failedResource of ['network', MODEL_IMAGE, GATEWAY_IMAGE]) {
    const f = await fixture(t);
    const resources = new Set();
    const original = f.dependencies.docker;
    f.dependencies.docker = async (args, options) => {
      const result = await original(args, options);
      if (args[0] === 'network' && args[1] === 'create') {
        resources.add(args.at(-1));
        if (failedResource === 'network') throw new Error('Create acknowledgement lost');
      } else if (args[0] === 'create') {
        resources.add(args[args.indexOf('--name') + 1]);
        if (args.includes(failedResource)) throw new Error('Create acknowledgement lost');
      } else if (args[0] === 'rm' || (args[0] === 'network' && args[1] === 'rm')) resources.delete(args.at(-1));
      return result;
    };
    await assert.rejects(startServices(f.options, f.dependencies), /acknowledgement lost/);
    assert.deepEqual([...resources], []);
    assert.deepEqual(f.calls.filter(c => c.args[0] === 'rm').map(c => c.args.at(-1)), [
      'claude-review-fixture-mcpg', 'claude-review-fixture-model',
    ]);
  }
});

test('image preparation pulls only pinned images with a longer timeout and no credential environment', async () => {
  const calls = [];
  await prepareImages({ docker: async (args, options) => { calls.push({ args, options }); } });
  assert.deepEqual(calls, [MODEL_IMAGE, GATEWAY_IMAGE, GITHUB_IMAGE, BUN_IMAGE].map(image => ({ args: ['pull', image], options: { timeout: 120000 } })));
});

test('failed readiness exits immediately after cancellation without pausing', async t => {
  const f = await fixture(t);
  const original = f.dependencies.docker;
  let pauses = 0;
  f.dependencies.pause = async () => { pauses++; };
  f.dependencies.docker = async (args, options) => {
    if (args[0] === 'exec') {
      process.emit('SIGTERM');
      throw new Error('Readiness failed during cancellation');
    }
    return original(args, options);
  };
  await assert.rejects(startServices(f.options, f.dependencies), /interrupted/);
  assert.equal(pauses, 0);
  assert.equal(f.calls.filter(c => c.args[0] === 'ps').length, 1);
  assert.deepEqual(f.calls.at(-1).args, ['network', 'rm', 'claude-review-fixture-agent']);
});

test('cleanup queries the exact session label and removes only validated full backend IDs', async t => {
  const f = await fixture(t);
  const original = f.dependencies.docker;
  const first = 'a'.repeat(64);
  const second = 'b'.repeat(64);
  f.dependencies.docker = async (args, options) => {
    const result = await original(args, options);
    return args[0] === 'ps' ? { stdout: `${first}\n${second}\n${first}\nshort-id\n--all\n${'C'.repeat(64)}\n` } : result;
  };
  const services = await startServices(f.options, f.dependencies);
  await services.close();
  assert.deepEqual(f.calls.find(c => c.args[0] === 'ps').args, [
    'ps', '--all', '--quiet', '--no-trunc', '--filter', 'label=gumnut.claude-review-session=fixture',
  ]);
  assert.deepEqual(f.calls.filter(c => c.args[0] === 'rm').map(c => c.args.at(-1)), [
    'claude-review-fixture-mcpg', 'claude-review-fixture-model', first, second,
  ]);
});

test('ambiguous gateway attachment cleans up every discovered session backend', async t => {
  const f = await fixture(t);
  const original = f.dependencies.docker;
  const backendIDs = ['c'.repeat(64), 'd'.repeat(64)];
  const resources = new Set(backendIDs);
  f.dependencies.attachGateway = () => { throw new Error('Gateway acknowledgement lost'); };
  f.dependencies.docker = async (args, options) => {
    const result = await original(args, options);
    if (args[0] === 'ps') return { stdout: backendIDs.join('\n') };
    if (args[0] === 'rm') resources.delete(args.at(-1));
    return result;
  };
  await assert.rejects(startServices(f.options, f.dependencies), /acknowledgement lost/);
  assert.deepEqual([...resources], []);
});
