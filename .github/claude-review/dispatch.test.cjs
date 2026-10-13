const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { dispatch, prepare, finish, createApi, writeOutput } = require('./dispatch.cjs');

const head = 'a'.repeat(40);
const fullName = 'gumnut-ai/immich-adapter';
function fixture(mode = 'prepare') {
  const pr = {
    number: 2645, state: 'open', draft: false,
    head: { sha: head, repo: { full_name: fullName } },
    base: { ref: 'main', repo: { full_name: fullName } },
  };
  const origin = {
    repository: { full_name: fullName }, event: 'pull_request_target',
    path: '.github/workflows/claude-review.yml', actor: { type: 'User', login: 'writer' },
    pull_requests: [{ number: 2645, head: { sha: head } }],
  };
  const env = {
    GITHUB_REPOSITORY: fullName, GITHUB_EVENT_NAME: mode === 'dispatch' ? 'pull_request_target' : 'workflow_dispatch',
    GITHUB_REF: 'refs/heads/main', GITHUB_RUN_ID: '12345', GITHUB_ACTOR: 'github-actions[bot]',
    CLAUDE_REVIEW_ENABLED: 'true',
  };
  const event = {
    repository: { full_name: fullName, default_branch: 'main' },
    sender: mode === 'dispatch' ? { type: 'User', login: 'writer' } : { type: 'Bot', login: 'github-actions[bot]' },
    inputs: { pr_number: '2645', expected_head: head, origin_run_id: '9876' }, pull_request: pr,
  };
  const calls = [];
  const outputs = [];
  const responses = { pr, origin, permission: { permission: 'write' } };
  const api = async (method, route, body) => {
    calls.push({ method, route, body });
    if (route.endsWith('/permission')) return responses.permission;
    if (route.includes('/pulls/')) return responses.pr;
    if (route.includes('/actions/runs/')) return responses.origin;
    if (route.endsWith('/check-runs')) return { id: 777 };
    return undefined;
  };
  return { env, event, api, output: values => outputs.push(values), calls, outputs, responses };
}
function writes(f) { return f.calls.filter(call => call.method !== 'GET'); }
async function rejected(mode, f) {
  let result;
  try {
    result = await (mode === 'dispatch' ? dispatch : prepare)(f);
  } catch (error) {
    assert.equal(error.message, 'Claude review routing could not be verified.');
  }
  if (result) assert.equal(result.eligible, false);
  assert.deepEqual(writes(f), []);
}

test('metadata dispatcher sends only validated routing inputs to the default branch', async () => {
  const f = fixture('dispatch');
  assert.deepEqual(await dispatch(f), { eligible: true });
  assert.deepEqual(writes(f), [{ method: 'POST', route: '/repos/gumnut-ai/immich-adapter/actions/workflows/claude-review.yml/dispatches',
    body: { ref: 'main', inputs: { pr_number: '2645', expected_head: head, origin_run_id: '12345' } } }]);
});

test('verified automatic dispatch creates a native check on the live PR head', async () => {
  const f = fixture();
  assert.deepEqual(await prepare(f), { eligible: true });
  assert.equal(f.calls.some(call => call.route.endsWith('/collaborators/writer/permission')), true);
  assert.deepEqual(writes(f), [{ method: 'POST', route: '/repos/gumnut-ai/immich-adapter/check-runs', body: {
    name: 'Claude PR review', head_sha: head, status: 'in_progress', details_url: 'https://github.com/gumnut-ai/immich-adapter/actions/runs/12345',
  } }]);
  assert.deepEqual(f.outputs, [{ eligible: 'true', pr_number: '2645', head_sha: head, check_id: '777', owner: 'gumnut-ai', repo: 'immich-adapter' }]);
});

test('GitHub bots can open and update eligible bot-authored PRs', async () => {
  for (const action of ['opened', 'synchronize', 'ready_for_review', 'reopened']) {
    const f = fixture('dispatch');
    f.event.action = action;
    f.event.sender = { type: 'Bot', login: 'gumnut-publisher[bot]' };
    f.event.pull_request.user = { type: 'Bot', login: 'gumnut-publisher[bot]' };
    f.responses.permission.permission = 'read';
    assert.deepEqual(await dispatch(f), { eligible: true });
    assert.equal(f.calls.some(call => call.route.includes('/collaborators/')), false);
    assert.deepEqual(writes(f)[0].body, {
      ref: 'main', inputs: { pr_number: '2645', expected_head: head, origin_run_id: '12345' },
    });
  }
});

test('automatic bot origins require the same authenticated run and PR binding', async () => {
  const f = fixture();
  f.responses.origin.actor = { type: 'Bot', login: 'gumnut-publisher[bot]' };
  assert.deepEqual(await prepare(f), { eligible: true });
  assert.equal(f.calls.some(call => call.route.endsWith('/actions/runs/9876')), true);
  assert.equal(f.calls.some(call => call.route.includes('/collaborators/')), false);
  for (const mutate of [
    f => { delete f.event.inputs.origin_run_id; },
    f => { delete f.event.inputs.expected_head; },
    f => { f.responses.origin.repository.full_name = 'other/immich-adapter'; },
    f => { f.responses.origin.event = 'workflow_dispatch'; },
    f => { f.responses.origin.path = '.github/workflows/other.yml'; },
    f => { f.responses.origin.pull_requests[0].number = 2646; },
    f => { f.responses.origin.pull_requests[0].head.sha = 'b'.repeat(40); },
  ]) {
    const rejectedOrigin = fixture();
    rejectedOrigin.responses.origin.actor = { type: 'Bot', login: 'gumnut-publisher[bot]' };
    mutate(rejectedOrigin);
    await rejected('prepare', rejectedOrigin);
  }
});

test('bot identity requires both GitHub Bot type and exact bot login syntax', async () => {
  for (const actor of [
    { type: 'Bot', login: 'gumnut-publisher' },
    { type: 'Bot', login: '[bot]' },
    { type: 'Bot', login: 'publisher[BOT]' },
    { type: 'Bot', login: 'publisher[bot]\n' },
    { type: 'Bot', login: 'publisher[bot]/other' },
    { type: 'Bot', login: 'publisher/../../other[bot]' },
    { type: 'Bot', login: null },
    { type: 'Bot' },
    { type: 'User', login: 'publisher[bot]' },
    { type: 'Organization', login: 'publisher[bot]' },
    { login: 'publisher[bot]' },
  ]) {
    for (const mode of ['dispatch', 'prepare']) {
      const f = fixture(mode);
      if (mode === 'dispatch') f.event.sender = actor;
      else f.responses.origin.actor = actor;
      await rejected(mode, f);
    }
  }
});

test('manual bot dispatch and forged GitHub Actions identities cannot create checks', async () => {
  for (const actor of [
    { type: 'Bot', login: 'publisher[bot]' },
    { type: 'Bot', login: 'github-actions[bot]' },
    { type: 'User', login: 'github-actions[bot]' },
    { type: 'Bot', login: 'github-actions' },
  ]) {
    const f = fixture();
    f.event.sender = actor;
    f.env.GITHUB_ACTOR = actor.login;
    f.event.inputs = { pr_number: '2645' };
    await rejected('prepare', f);
  }
});

test('human manual dispatch permits writer, maintainer and admin and optional head', async () => {
  for (const permission of ['write', 'maintain', 'admin']) {
    const f = fixture();
    f.event.sender = { type: 'User', login: 'writer' };
    f.env.GITHUB_ACTOR = 'writer';
    f.event.inputs = { pr_number: '2645' };
    f.responses.permission.permission = permission;
    assert.deepEqual(await prepare(f), { eligible: true });
    assert.equal(f.calls.some(call => call.route.includes('/actions/runs/')), false);
  }
});

test('forks, drafts, closed PRs, other repositories and bases have no write side effects', async () => {
  for (const mode of ['dispatch', 'prepare']) {
    for (const actorType of ['User', 'Bot']) {
      for (const mutate of [
        f => { f.responses.pr.head.repo.full_name = 'fork/photos'; },
        f => { f.responses.pr.base.repo.full_name = 'fork/photos'; },
        f => { f.responses.pr.base.ref = 'release'; },
        f => { f.responses.pr.state = 'closed'; },
        f => { f.responses.pr.draft = true; },
        f => { f.event.repository.full_name = 'fork/photos'; },
      ]) {
        const f = fixture(mode);
        const actor = { type: actorType, login: actorType === 'Bot' ? 'publisher[bot]' : 'writer' };
        if (mode === 'dispatch') f.event.sender = actor;
        else f.responses.origin.actor = actor;
        mutate(f);
        await rejected(mode, f);
      }
    }
  }
  const f = fixture();
  f.responses.pr.number = 2646;
  await rejected('prepare', f);
});

test('disabled setting and nonwriters cannot dispatch or create checks', async () => {
  for (const mode of ['dispatch', 'prepare']) {
    for (const actorType of ['User', 'Bot']) {
      for (const value of [undefined, 'false', 'TRUE', 'true\n']) {
        const f = fixture(mode);
        const actor = { type: actorType, login: actorType === 'Bot' ? 'publisher[bot]' : 'writer' };
        if (mode === 'dispatch') f.event.sender = actor;
        else f.responses.origin.actor = actor;
        f.env.CLAUDE_REVIEW_ENABLED = value;
        await rejected(mode, f);
        assert.deepEqual(f.calls, []);
      }
    }
    for (const permission of ['read', 'triage', undefined]) {
      const f = fixture(mode);
      f.responses.permission.permission = permission;
      await rejected(mode, f);
    }
  }
});

test('stale worker head skips before check creation', async () => {
  const f = fixture();
  f.responses.pr.head.sha = 'b'.repeat(40);
  await rejected('prepare', f);
  assert.deepEqual(f.outputs, [{ eligible: 'false' }]);
});

test('automatic origins must prove the exact repository, workflow, event, actor and PR head', async () => {
  for (const mutate of [
    f => { f.event.inputs.origin_run_id = ''; },
    f => { f.event.inputs.expected_head = ''; },
    f => { f.responses.origin.repository.full_name = 'other/immich-adapter'; },
    f => { f.responses.origin.event = 'push'; },
    f => { f.responses.origin.path = '.github/workflows/other.yml'; },
    f => { f.responses.origin.path = '.github/workflows/claude-review.yml@contributor'; },
    f => { f.responses.origin.actor.type = 'Bot'; },
    f => { f.responses.origin.pull_requests[0].number = 2646; },
    f => { f.responses.origin.pull_requests[0].head.sha = 'b'.repeat(40); },
    f => { f.responses.origin.pull_requests = []; },
    f => { f.responses.origin.actor.login = 'writer/../../other'; },
  ]) {
    const f = fixture();
    mutate(f);
    await rejected('prepare', f);
  }
});

test('only the GitHub Actions bot with provenance or the matching human actor is trusted', async () => {
  for (const mutate of [
    f => { f.env.GITHUB_ACTOR = 'other[bot]'; f.event.sender.login = 'other[bot]'; },
    f => { f.event.sender.type = 'User'; },
    f => { f.event.sender.login = 'another'; },
    f => { f.env.GITHUB_ACTOR = 'writer'; f.event.sender = { type: 'Bot', login: 'writer' }; delete f.event.inputs.origin_run_id; },
    f => { f.env.GITHUB_ACTOR = 'writer'; f.event.sender = { type: 'User', login: 'another' }; delete f.event.inputs.origin_run_id; },
  ]) {
    const f = fixture();
    mutate(f);
    await rejected('prepare', f);
  }
  const f = fixture('dispatch');
  f.event.sender.type = 'Bot';
  await rejected('dispatch', f);
});

test('malformed identifiers, hashes and untrusted refs never cause writes', async () => {
  for (const mode of ['dispatch', 'prepare']) {
    for (const value of ['0', '-1', '1e3', '1\ncheck_id=99', '9007199254740992', '01', '1.5', null]) {
      const f = fixture(mode);
      if (mode === 'dispatch') f.event.pull_request.number = value;
      else f.event.inputs.pr_number = value;
      await rejected(mode, f);
    }
    for (const value of ['x'.repeat(40), 'a'.repeat(39), `${head}\n`]) {
      const f = fixture(mode);
      if (mode === 'dispatch') f.event.pull_request.head.sha = value;
      else f.event.inputs.expected_head = value;
      await rejected(mode, f);
    }
    for (const value of ['owner/repo/other', '../photos', 'owner/..', 'owner/repo\n']) {
      const f = fixture(mode);
      f.env.GITHUB_REPOSITORY = value;
      await rejected(mode, f);
    }
    const f = fixture(mode);
    f.env.GITHUB_EVENT_NAME = 'push';
    await rejected(mode, f);
  }
  for (const value of ['refs/heads/pr-branch', 'refs/tags/main', undefined]) {
    const f = fixture();
    f.env.GITHUB_REF = value;
    await rejected('prepare', f);
  }
  for (const value of ['0', '9007199254740992', '9876\n']) {
    const f = fixture();
    f.event.inputs.origin_run_id = value;
    await rejected('prepare', f);
  }
  for (const mode of ['dispatch', 'prepare']) {
    const f = fixture(mode);
    f.env.GITHUB_RUN_ID = '0';
    await rejected(mode, f);
  }
});

test('native check success requires successful job and successful inference', async () => {
  for (const [status, conclusion, expected] of [
    ['success', 'success', 'success'], ['success', 'failure', 'failure'], ['success', undefined, 'failure'],
    ['failure', 'success', 'failure'], ['cancelled', 'success', 'cancelled'], ['skipped', 'success', 'failure'],
  ]) {
    const f = fixture();
    Object.assign(f.env, { CHECK_ID: '777', REVIEW_JOB_STATUS: status, CLAUDE_CONCLUSION: conclusion });
    await finish(f);
    assert.deepEqual(writes(f), [{ method: 'PATCH', route: '/repos/gumnut-ai/immich-adapter/check-runs/777', body: { status: 'completed', conclusion: expected } }]);
  }
});

test('finish refuses malformed check IDs and untrusted execution refs', async () => {
  for (const mutate of [
    f => { f.env.CHECK_ID = '0'; },
    f => { f.env.CHECK_ID = '777\n'; },
    f => { f.env.GITHUB_REF = 'refs/heads/contributor'; },
    f => { f.env.GITHUB_EVENT_NAME = 'pull_request_target'; },
  ]) {
    const f = fixture();
    f.env.CHECK_ID = '777';
    mutate(f);
    await assert.rejects(finish(f), { message: 'Claude review routing could not be verified.' });
    assert.deepEqual(writes(f), []);
  }
});

test('API handles empty dispatch responses and JSON check responses', async () => {
  const api = createApi({ GH_TOKEN: 'SECRET' }, async (_url, options) => {
    assert.deepEqual(JSON.parse(options.body), { safe: 'value' });
    return options.method === 'POST' ? { ok: true, status: 204 } :
      { ok: true, status: 200, json: async () => ({ id: 777 }) };
  });
  assert.equal(await api('POST', '/repos/gumnut-ai/immich-adapter/actions/workflows/claude-review.yml/dispatches', { safe: 'value' }), undefined);
  assert.deepEqual(await api('PATCH', '/repos/gumnut-ai/immich-adapter/check-runs/777', { safe: 'value' }), { id: 777 });
});

test('API uses scoped headers and timeout and never exposes or retries uncertain failures', async () => {
  let calls = 0;
  const api = createApi({ GH_TOKEN: 'SECRET' }, async (url, options) => {
    calls++;
    assert.equal(url, 'https://api.github.com/repos/gumnut-ai/immich-adapter/check-runs');
    assert.equal(options.headers.Authorization, 'Bearer SECRET');
    assert.equal(options.headers.Accept, 'application/vnd.github+json');
    assert.equal(options.headers['X-GitHub-Api-Version'], '2022-11-28');
    assert.ok(options.signal instanceof AbortSignal);
    throw new Error('SECRET upstream detail');
  });
  await assert.rejects(api('POST', '/repos/gumnut-ai/immich-adapter/check-runs', {}), { message: 'Claude review routing could not be verified.' });
  assert.equal(calls, 1);
  const denied = createApi({ GH_TOKEN: 'SECRET' }, async () => ({ ok: false, json: () => { throw new Error('SECRET'); } }));
  await assert.rejects(denied('GET', '/repos/gumnut-ai/immich-adapter'), { message: 'Claude review routing could not be verified.' });
});

test('outputs reject newline injection and CLI hides parse errors and credentials', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'claude-dispatch-'));
  try {
    const output = path.join(dir, 'output');
    assert.throws(() => writeOutput(output, { eligible: 'true\ncheck_id=99' }));
    assert.equal(fs.existsSync(output), false);
    writeOutput(output, { eligible: 'false' });
    assert.equal(fs.readFileSync(output, 'utf8'), 'eligible=false\n');
    const event = path.join(dir, 'event.json');
    fs.writeFileSync(event, 'SECRET malformed JSON');
    const result = spawnSync(process.execPath, [path.join(__dirname, 'dispatch.cjs'), 'prepare'], {
      env: { GITHUB_EVENT_PATH: event, GH_TOKEN: 'SECRET' }, encoding: 'utf8',
    });
    assert.equal(result.status, 1);
    assert.equal(result.stdout, '');
    assert.equal(result.stderr, '::error::Claude review routing could not be verified.\n');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('origin path accepts GitHub bare and default-ref forms only', async () => {
  for (const path of ['.github/workflows/claude-review.yml', '.github/workflows/claude-review.yml@main', '.github/workflows/claude-review.yml@refs/heads/main']) {
    const f = fixture();
    f.responses.origin.path = path;
    assert.deepEqual(await prepare(f), { eligible: true });
  }
});
