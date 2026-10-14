const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { source, route, assessMany } = require('./request.cjs');
const repo = 'example/repo';
function fixture(eventName = 'pull_request_target') {
  const pr = { number: 10, state: 'open', draft: false, head: { repo: { full_name: repo } }, base: { ref: 'main', repo: { full_name: repo } } };
  const run = { id: 12, run_attempt: 1, status: 'completed', conclusion: 'success', repository: { full_name: repo }, head_repository: { full_name: repo }, path: '.github/workflows/pr-approval-request.yml', head_branch: 'main', event: 'issue_comment', display_title: '{"number":"10"}', pull_requests: [{ number: 10 }] };
  const env = { GITHUB_REPOSITORY: repo, GITHUB_REF: 'refs/heads/main', GITHUB_EVENT_NAME: eventName, GITHUB_ACTOR: 'gumnut-bot[bot]', PR_APPROVAL_MODE: 'shadow' };
  const event = { repository: { full_name: repo, default_branch: 'main' }, action: 'opened', pull_request: pr, sender: { type: 'Bot', login: 'gumnut-bot[bot]' }, issue: { number: 10, pull_request: {} }, comment: { id: 30, user: { type: 'Bot', login: 'claude[bot]' } }, inputs: { pr_number: '10' }, workflow_run: { id: 12 } };
  const job = { run_id: 12, run_attempt: 1, name: 'Approval request', status: 'completed', conclusion: 'success', steps: [{ name: 'Admitted approval request', status: 'completed', conclusion: 'success' }] };
  const responses = { jobs: [job], pr, run, prs: [pr], permission: 'write', comment: { user: event.comment.user, issue_url: `https://api.github.com/repos/${repo}/issues/10` } };
  const calls = [];
  const api = async (method, url) => {
    calls.push({ method, url });
    assert.equal(method, 'GET', 'Routing must never publish or dispatch');
    if (url.endsWith('/permission')) return { permission: responses.permission };
    if (url.includes('/jobs?')) {
      const page = Number(new URL(url, 'https://api.github.com').searchParams.get('page'));
      return { jobs: responses.jobs.slice((page - 1) * 100, page * 100), total_count: responses.jobs.length };
    }
    if (url.includes('/actions/runs/')) return responses.run;
    if (url.includes('/issues/comments/')) return responses.comment;
    if (url.includes('/pulls?')) return responses.prs;
    if (url.endsWith('/pulls/10')) return responses.pr;
    throw new Error('Unexpected route');
  };
  return { env, event, api, responses, calls };
}
async function denied(fn, f) {
  let result;
  try { result = await fn(f); } catch (error) { assert.ok(error instanceof Error); }
  if (result) assert.equal(result.eligible, false);
  assert.ok(f.calls.every(c => c.method === 'GET'));
}
test('same-repository bot event and human writer both request assessment', async () => {
  const bot = fixture();
  assert.deepEqual(await source(bot), { eligible: true, number: 10 });
  assert.ok(bot.calls.every(c => !c.url.endsWith('/permission')));
  const human = fixture();
  human.event.sender = { type: 'User', login: 'writer' };
  human.env.GITHUB_ACTOR = 'writer';
  assert.deepEqual(await source(human), { eligible: true, number: 10 });
  human.responses.permission = 'read';
  await denied(source, human);
});
test('unsafe PRs, wrong branch, malformed bot identities and disabled modes skip', async () => {
  for (const mutate of [
    f => { f.responses.pr.draft = true; }, f => { f.responses.pr.state = 'closed'; },
    f => { f.responses.pr.head.repo.full_name = 'attacker/repo'; },
    f => { f.responses.pr.base.ref = 'other'; }, f => { f.env.GITHUB_REF = 'refs/heads/other'; },
    f => { f.event.sender.login = 'not-a-bot'; f.env.GITHUB_ACTOR = 'not-a-bot'; },
    f => { f.env.PR_APPROVAL_MODE = 'true'; }, f => { f.event.action = 'edited'; },
  ]) { const f = fixture(); mutate(f); await denied(source, f); }
});
test('only authenticated reviewer comments can request assessment', async () => {
  for (const login of ['claude[bot]', 'chatgpt-codex-connector[bot]']) {
    const f = fixture('issue_comment');
    f.event.action = 'created';
    f.event.comment.user.login = login;
    assert.deepEqual(await source(f), { eligible: true, number: 10 });
  }
  for (const mutate of [
    f => { f.event.comment.user.type = 'User'; },
    f => { f.event.comment.user.login = 'other[bot]'; },
    f => { f.responses.comment.issue_url = 'https://api.github.com/repos/attacker/repo/issues/10'; },
    f => { delete f.event.issue.pull_request; },
  ]) { const f = fixture('issue_comment'); f.event.action = 'edited'; mutate(f); await denied(source, f); }
});
test('manual assessment requires a human writer at the default branch', async () => {
  const f = fixture('workflow_dispatch');
  await denied(source, f);
  f.event.sender = { type: 'User', login: 'writer' }; f.env.GITHUB_ACTOR = 'writer';
  assert.deepEqual(await source(f), { eligible: true, number: 10 });
  f.event.inputs.pr_number = '10\nPUBLISH_TOKEN=bad'; await denied(source, f);
});
test('deferred source routing re-fetches provenance and live eligibility', async () => {
  const f = fixture('workflow_run'); f.event.action = 'completed';
  assert.deepEqual(await route(f), { eligible: true, number: 10 });
  assert.equal(f.calls[0].url, `/repos/${repo}/actions/runs/12`);
  for (const mutate of [
    f => { f.responses.run.id = 99; }, f => { f.responses.run.repository.full_name = 'attacker/repo'; },
    f => { f.responses.run.head_repository.full_name = 'attacker/repo'; },
    f => { f.responses.run.conclusion = 'failure'; }, f => { f.responses.run.head_branch = 'contributor'; },
    f => { f.responses.run.path += '@contributor'; }, f => { f.responses.run.display_title = '{"number":"10","approve":true}'; },
    f => { f.responses.pr.draft = true; },
  ]) { const f = fixture('workflow_run'); f.event.action = 'completed'; mutate(f); await denied(route, f); }
});
test('associated CI requests reassessment without granting approval authority', async () => {
  const f = fixture('workflow_run'); f.event.action = 'completed';
  f.responses.run.path = '.github/workflows/python-app.yml';
  f.responses.run.event = 'pull_request'; f.responses.run.conclusion = 'failure';
  assert.deepEqual(await route(f), { eligible: true, number: 10 });
  f.responses.run.pull_requests = [];
  assert.deepEqual(await route(f), { eligible: false });
  f.responses.run.path = '.github/workflows/pr-approval-coordinator.yml';
  assert.deepEqual(await route(f), { eligible: false });
});
test('formal and inline review events assess live feedback without admitting self loops', async () => {
  for (const eventName of ['pull_request_review', 'pull_request_review_comment']) {
    const f = fixture(eventName);
    f.env.GITHUB_REF = 'refs/pull/10/merge';
    f.event.action = eventName === 'pull_request_review' ? 'submitted' : 'created';
    f.event.sender = { type: 'Bot', login: 'chatgpt-codex-connector[bot]' };
    f.event.review = { user: { ...f.event.sender } };
    f.event.comment.user = { ...f.event.sender };
    assert.deepEqual(await source(f), { eligible: true, number: 10 });
    f.event.review.user.login = 'gumnut-reviewer[bot]';
    f.event.comment.user.login = 'gumnut-reviewer[bot]';
    await denied(source, f);
  }
  const f = fixture('pull_request_review');
  f.event.action = 'dismissed'; f.env.GITHUB_REF = 'refs/pull/10/merge';
  f.event.review = { user: { type: 'User', login: 'reviewer' } };
  f.event.sender = { type: 'User', login: 'writer' };
  assert.deepEqual(await source(f), { eligible: true, number: 10 });
  f.responses.permission = 'read'; await denied(source, f);
});
test('review-event origin uses authenticated PR association, never a contributor result as approval', async () => {
  const f = fixture('workflow_run'); f.event.action = 'completed';
  f.responses.run.event = 'pull_request_review'; f.responses.run.head_branch = 'contributor';
  assert.deepEqual(await route(f), { eligible: true, number: 10 });
  f.responses.run.pull_requests = [{ number: 11 }]; await denied(route, f);
});
test('scheduled reassessment selects eligible open PRs including bots', async () => {
  const f = fixture('schedule');
  assert.deepEqual(await route(f), { eligible: true, numbers: [10] });
  f.responses.pr.draft = true;
  assert.deepEqual(await route(f), { eligible: false, numbers: [] });
  f.responses.pr.draft = false; f.responses.pr.head.repo.full_name = 'attacker/repo';
  assert.deepEqual(await route(f), { eligible: false, numbers: [] });
});
test('ordinary human inline replies do not schedule a decision assessment', async () => {
  const f = fixture('pull_request_review_comment'); f.event.action = 'created';
  f.env.GITHUB_REF = 'refs/pull/10/merge';
  f.event.comment.user = { type: 'User', login: 'writer' };
  f.event.sender = { type: 'User', login: 'writer' };
  assert.deepEqual(await source(f), { eligible: false });
});
test('one unavailable PR assessment does not stop the remaining sweep', async () => {
  const visited = [];
  const api = async (method, url) => {
    assert.equal(method, 'GET');
    if (url.endsWith('/pulls/10')) { visited.push(10); throw new Error('unavailable private evidence'); }
    if (url.endsWith('/pulls/11')) { visited.push(11); return { state: 'closed', draft: false, head: { sha: 'a'.repeat(40) }, base: { sha: 'b'.repeat(40) } }; }
    if (url === `/repos/${repo}`) return { default_branch: 'main' };
    throw new Error('Unexpected route');
  };
  const result = await assessMany({ api, repo, bot: 'gumnut-reviewer[bot]', numbers: [10, 11, 11] });
  assert.deepEqual(visited, [10, 11]);
  assert.deepEqual(result.failures, [10]);
  assert.deepEqual(result.numbers, []);
  assert.equal(result.results[0].decision, 'waiting');
  assert.equal(result.results[1].decision, 'ineligible');
  assert.ok(!JSON.stringify(result).includes('private evidence'));
});
test('workflow boundaries keep publisher credentials out of source and shadow jobs', () => {
  const request = fs.readFileSync(path.join(__dirname, '../workflows/pr-approval-request.yml'), 'utf8');
  const coordinator = fs.readFileSync(path.join(__dirname, '../workflows/pr-approval-coordinator.yml'), 'utf8');
  assert.doesNotMatch(request, /secrets\.|: write|head.sha|refs\/pull/);
  assert.match(request, /PR_APPROVAL_MODE == 'shadow'/);
  assert.match(request, /pull_request_review:\n/);
  assert.match(request, /name: Validate metadata request\n        id: request/);
  assert.match(request, /name: Admitted approval request\n        if: steps.request.outputs.eligible == 'true'/);
  assert.match(coordinator, /workflow_run.conclusion == 'success'/);
  assert.match(request, /pull_request_review_comment:\n/);
  assert.match(request, /ref: \$\{\{ github.event.repository.default_branch \}\}/);
  assert.match(coordinator, /workflow_run:/);
  assert.match(coordinator, /schedule:/);
  const assessment = coordinator.split('  assess:')[1].split('  publish:')[0];
  const publication = coordinator.split('  publish:')[1];
  assert.doesNotMatch(assessment, /secrets\.|create-github-app-token|PUBLISH_TOKEN/);
  assert.match(publication, /vars.PR_APPROVAL_MODE == 'publish'/);
  assert.match(publication, /outputs.actionable == 'true'/);
  assert.match(publication, /environment: pr-approval-publisher/);
  assert.match(publication, /cancel-in-progress: false/);
  assert.match(publication, /permission-pull-requests: write/);
  assert.match(publication, /app-id: \$\{\{ vars.PR_APPROVAL_APP_ID \}\}/);
  assert.doesNotMatch(publication, /client-id:/);
  assert.doesNotMatch(publication, /permission-contents: write|persist-credentials: true/);
  assert.match(coordinator, /name != 'PR approval coordinator'/);
  assert.doesNotMatch(coordinator, /download-artifact|pull_request_target|issue_comment/);
});

test('unsuccessful request runs skip without reading jobs or PR metadata', async () => {
  for (const conclusion of ['skipped', 'failure', 'cancelled', null]) {
    const f = fixture('workflow_run'); f.event.action = 'completed';
    f.responses.run.conclusion = conclusion; f.responses.run.display_title = 'not JSON';
    assert.deepEqual(await route(f), { eligible: false });
    assert.equal(f.calls.length, 1);
  }
});
test('successful no-op requests do not cross the deferred admission gate', async () => {
  for (const noOp of ['read-only review', 'non-base edit']) {
    const f = fixture(noOp === 'read-only review' ? 'pull_request_review' : 'pull_request_target');
    if (noOp === 'read-only review') {
      f.event.action = 'submitted'; f.event.review = { user: { type: 'User', login: 'reader' } };
      f.event.sender = { type: 'User', login: 'reader' }; f.responses.permission = 'read';
    } else f.event.action = 'edited';
    assert.deepEqual(await source(f), { eligible: false });
    f.env.GITHUB_EVENT_NAME = 'workflow_run'; f.event.action = 'completed';
    f.responses.jobs[0].steps[0].conclusion = 'skipped';
    f.calls.length = 0;
    assert.deepEqual(await route(f), { eligible: false });
    assert.equal(f.calls.length, 2);
  }
});
test('admission receipt must belong to the successful exact run attempt', async () => {
  for (const mutate of [
    f => { f.responses.jobs = []; }, f => { f.responses.jobs[0].steps = []; },
    f => { f.responses.jobs[0].run_id = 13; }, f => { f.responses.jobs[0].run_attempt = 2; },
    f => { f.responses.jobs[0].name = 'Other job'; }, f => { f.responses.jobs[0].status = 'in_progress'; },
    f => { f.responses.jobs[0].conclusion = 'failure'; },
    f => { f.responses.jobs[0].steps[0].name = 'Other step'; },
    f => { f.responses.jobs[0].steps[0].status = 'in_progress'; },
    f => { f.responses.jobs[0].steps[0].conclusion = 'failure'; },
    f => { delete f.responses.run.run_attempt; },
  ]) {
    const f = fixture('workflow_run'); f.event.action = 'completed'; mutate(f);
    await denied(route, f);
    assert.ok(!f.calls.some(c => c.url.endsWith('/pulls/10')));
  }
});
test('admission jobs are completely paginated before routing', async () => {
  const f = fixture('workflow_run'); f.event.action = 'completed';
  f.responses.jobs = [...Array.from({ length: 100 }, () => ({ name: 'Other job' })), ...f.responses.jobs];
  assert.deepEqual(await route(f), { eligible: true, number: 10 });
  assert.equal(f.calls.filter(c => c.url.includes('/jobs?')).length, 2);
});
