const { test } = require('node:test');
const assert = require('node:assert/strict');
const { evaluate, publish, createApi, pages } = require('./coordinator.cjs');
const hostedCodex = require('./fixtures/codex-output.json');
// These tests must never contact GitHub, including accidental route fallthrough.
globalThis.fetch = async () => { throw new Error('Network forbidden in coordinator tests'); };
const head = 'a'.repeat(40), base = 'b'.repeat(40), old = 'c'.repeat(40);
const repo = 'example/repo', bot = 'gumnut-approval[bot]';
const runURL = `https://github.com/${repo}/actions/runs/123`;
const user = login => ({ login, type: 'Bot', id: login === 'chatgpt-codex-connector[bot]' ? 199175422 : 123 });
const comment = (login, body, id = 1) => ({ id, user: user(login), body, created_at: `2026-10-12T00:${String(id).padStart(2, '0')}:00Z`, html_url: `https://github.com/${repo}/pull/7#issuecomment-${id}` });
const claude = (verdict = 'clean', id = 1) => comment('claude[bot]', `Review completed.\n<!-- gumnut-review-verdict/v1 head=${old} base=${old} verdict=${verdict} -->`, id);
const codex = () => comment('chatgpt-codex-connector[bot]', `Codex Review: Didn't find any major issues.\n\n**Reviewed commit:** \`${old.slice(0, 7)}\``, 2);
const review = (login, state, id = login === 'chatgpt-codex-connector[bot]' ? 4 : 3) => ({ id, user: user(login), state, commit_id: old, submitted_at: `2026-10-12T00:${String(id).padStart(2, '0')}:00Z`, body: '', html_url: 'https://github.com/review' });
const graphReview = r => ({ databaseId: r.id, state: r.state, body: r.body, submittedAt: r.submitted_at, updatedAt: r.submitted_at, lastEditedAt: null, commit: { oid: r.commit_id }, author: { login: r.user.login.replace('[bot]', ''), __typename: r.user.type } });
const workflowRun = (overrides = {}) => ({ id: 1, workflow_id: 10, run_attempt: 1, created_at: '2026-10-12T01:00:00Z', head_sha: head, status: 'completed', conclusion: 'success', ...overrides });
const conn = (nodes, hasNextPage = false, endCursor = null) => ({ nodes, pageInfo: { hasNextPage, endCursor } });
function fixture(options = {}) {
  const pr = { state: 'open', draft: false, user: { login: 'author' }, head: { sha: head, repo: { full_name: repo } }, base: { sha: base, ref: 'main', repo: { full_name: repo } }, ...options.pr };
  const comments = options.comments ?? [claude(), codex()];
  const reviews = options.reviews ?? [];
  const checks = options.checks ?? [{ name: 'Tests', status: 'completed', conclusion: 'success' }];
  const statuses = options.statuses ?? [];
  const runs = (options.runs ?? []).map((r, i) => workflowRun({ id: i + 1, workflow_id: i + 10, ...r }));
  const calls = [];
  const api = async (method, route, body) => {
    calls.push({ method, route, body });
    if (options.override) { const response = await options.override(method, route, body); if (response !== undefined) return response; }
    if (route === '/graphql') {
      if (body.query.includes('reviews(first:100')) return { data: { repository: { pullRequest: { reviews: conn(options.graphReviews ?? reviews.map(graphReview)) } } } };
      return { data: { repository: { pullRequest: { reviewThreads: conn(options.threads ?? []) } } } };
    }
    if (route === `/repos/${repo}`) return { default_branch: 'main' };
    if (route === `/repos/${repo}/pulls/7`) return pr;
    const page = Number(new URL(route, 'https://api.github.com').searchParams.get('page'));
    const slice = values => values.slice((page - 1) * 100, page * 100);
    if (route.includes('/reactions?')) return slice(options.reactions ?? []);
    if (route.includes('/reviews?')) return slice(reviews);
    if (route.includes('/comments?')) return slice(comments);
    if (route.includes('/check-runs?')) return { total_count: checks.length, check_runs: slice(checks) };
    if (route.includes('/status?')) return { total_count: statuses.length, statuses: slice(statuses) };
    if (route.includes('/actions/runs?')) return { total_count: runs.length, workflow_runs: slice(runs) };
    throw new Error(`Unexpected fixture request: ${route}`);
  };
  return { api, repo, number: 7, bot, calls };
}
test('standing older reviewer signoffs approve with current-head CI', async () => {
  const result = await evaluate(fixture());
  assert.equal(result.decision, 'approve');
  assert.equal(result.signoffs.claude.commit, old);
  assert.equal(result.signoffs.codex.commit, old.slice(0, 7));
});
test('incomplete Claude run preserves prior clean and prior concerns', async () => {
  assert.equal((await evaluate(fixture({ comments: [claude(), codex(), claude('incomplete', 4)] }))).decision, 'approve');
  assert.equal((await evaluate(fixture({ comments: [claude('concerns'), codex(), claude('incomplete', 4)] }))).decision, 'concerns');
});
test('latest structured concern supersedes clean; clean may supersede concern', async () => {
  assert.equal((await evaluate(fixture({ comments: [claude(), codex(), claude('concerns', 4)] }))).decision, 'concerns');
  assert.equal((await evaluate(fixture({ comments: [claude('concerns'), codex(), claude('clean', 4)] }))).decision, 'approve');
});
test('edited issue verdicts order by authenticated updated_at', async () => {
  const edited = { ...claude('concerns', 1), updated_at: '2026-10-12T00:10:00Z' };
  assert.equal((await evaluate(fixture({ comments: [edited, codex(), claude('clean', 4)] }))).decision, 'concerns');
  const clean = { ...claude('clean', 1), updated_at: '2026-10-12T00:10:00Z' };
  assert.equal((await evaluate(fixture({ comments: [clean, codex(), claude('concerns', 4)] }))).decision, 'approve');
  const formal = { ...review('claude[bot]', 'CHANGES_REQUESTED', 3), updated_at: '2026-10-12T00:20:00Z' };
  // A formal changes request remains blocking regardless of later comments.
  assert.equal((await evaluate(fixture({ comments: [clean, codex()], reviews: [formal] }))).decision, 'concerns');
});
test('significant body-only COMMENTED review supersedes earlier clean and later clean clears it', async () => {
  for (const [login, body] of [['claude[bot]', '<!-- gumnut-review-finding/v1 severity=significant -->'], ['claude[bot]', 'This should be fixed before merge'], ['chatgpt-codex-connector[bot]', '**[P1] fix this**']]) {
    const finding = { ...review(login, 'COMMENTED', 3), body };
    assert.equal((await evaluate(fixture({ reviews: [finding] }))).decision, 'concerns');
    const clean = login === 'claude[bot]' ? claude('clean', 4) : { ...codex(), id: 4, created_at: '2026-10-12T00:04:00Z' };
    assert.equal((await evaluate(fixture({ reviews: [finding], comments: [claude(), codex(), clean] }))).decision, 'approve');
  }
});
test('same-second source decisions retain concerns regardless of cross-namespace IDs', async () => {
  const time = '2026-10-12T00:10:00Z';
  for (const [commentId, reviewId] of [[6133000000, 5495000000], [10, 20]]) {
    const clean = { ...codex(), id: commentId, created_at: time, updated_at: time };
    const finding = { ...review('chatgpt-codex-connector[bot]', 'COMMENTED', 4), id: reviewId, body: '[P1] significant concern' };
    const meta = { ...graphReview(finding), updatedAt: time, lastEditedAt: time };
    assert.equal((await evaluate(fixture({ comments: [claude(), clean], reviews: [finding], graphReviews: [meta] }))).decision, 'concerns');
    clean.updated_at = '2026-10-12T00:11:00Z';
    assert.equal((await evaluate(fixture({ comments: [claude(), clean], reviews: [finding], graphReviews: [meta] }))).decision, 'approve');
  }
});
test('malformed, quoted, duplicate, and legacy Claude markers cannot establish approval', async () => {
  for (const body of ['looks good', `> <!-- gumnut-review-verdict/v1 head=${old} base=${old} verdict=clean -->`, '```\n' + claude().body + '\n```', claude().body + '\n' + claude().body, '<!-- gumnut-review-verdict/v2 verdict=clean -->']) {
    assert.equal((await evaluate(fixture({ comments: [claude(), codex(), comment('claude[bot]', body, 4)] }))).decision, 'concerns');
  }
  assert.equal((await evaluate(fixture({ comments: [comment('claude[bot]', 'No major issues'), codex()] }))).decision, 'concerns');
});
test('Codex summary and duplicate commit declarations do not count', async () => {
  for (const body of ['| Codex Review | Completed |', codex().body + '\n**Reviewed commit:** `abcdef0`']) assert.equal((await evaluate(fixture({ comments: [claude(), comment('chatgpt-codex-connector[bot]', body)] }))).decision, 'concerns');
});
test('unknown later source feedback blocks earlier clean decisions, including formal bodies', async () => {
  for (const login of ['claude[bot]', 'chatgpt-codex-connector[bot]']) {
    const unknown = comment(login, 'New review format: a consequential regression remains.', 4);
    assert.equal((await evaluate(fixture({ comments: [claude(), codex(), unknown] }))).decision, 'concerns');
    const formal = { ...review(login, 'COMMENTED', 4), body: unknown.body };
    assert.equal((await evaluate(fixture({ reviews: [formal] }))).decision, 'concerns');
    const clean = login === 'claude[bot]' ? claude('clean', 5) : { ...codex(), id: 5, created_at: '2026-10-12T00:05:00Z' };
    assert.equal((await evaluate(fixture({ comments: [claude(), codex(), unknown, clean] }))).decision, 'approve');
    assert.equal((await evaluate(fixture({ comments: [claude(), codex(), unknown, claude('incomplete', 5)] }))).decision, 'concerns');
  }
});
test('authenticated activity tables preserve standing decisions and cannot establish a signoff', async () => {
  for (const body of [hostedCodex.activity, hostedCodex.runningActivity, hostedCodex.automaticActivity]) {
    const activity = comment('chatgpt-codex-connector[bot]', body, 4);
    assert.equal((await evaluate(fixture({ comments: [claude(), codex(), activity] }))).decision, 'approve');
    assert.equal((await evaluate(fixture({ comments: [claude(), activity] }))).decision, 'waiting');
    assert.equal((await evaluate(fixture({ comments: [claude('concerns'), codex(), activity] }))).decision, 'concerns');
  }
});
test('automatic activity still rejects unknown triggers and substantive appended feedback', async () => {
  for (const body of [hostedCodex.automaticActivity.replace('| PR opened |', '| Unknown trigger |'), hostedCodex.automaticActivity + '\n[P1] A remaining concern']) {
    const activity = comment('chatgpt-codex-connector[bot]', body, 4);
    assert.equal((await evaluate(fixture({ comments: [claude(), codex(), activity] }))).decision, 'concerns');
  }
});
test('native PR thumbs-up signs off only through an authenticated Bot account ID', async () => {
  const activity = comment('chatgpt-codex-connector[bot]', hostedCodex.automaticActivity, 2);
  const f = fixture({ comments: [claude(), activity], reactions: [hostedCodex.prReaction] });
  const result = await evaluate(f);
  assert.equal(result.decision, 'approve');
  assert.equal(result.signoffs.codex.kind, 'pr-reaction');
  assert.equal(result.signoffs.codex.commit, null);
  assert.equal(result.signoffs.codex.reactionId, hostedCodex.prReaction.id);
  assert.ok(f.calls.some(c => c.route.includes('/issues/7/reactions?')));
  assert.ok(!f.calls.some(c => c.route.includes('/comments/') && c.route.includes('/reactions')));
  for (const mutate of [
    f => { f.comments = [claude()]; },
    f => { f.activity.user.type = 'User'; },
    f => { f.activity.user.id = 999; },
    f => { f.reaction.user = { ...f.reaction.user, id: 999 }; },
    f => { f.reaction.user = { ...f.reaction.user, login: 'another[bot]' }; },
    f => { f.reaction.content = 'heart'; },
  ]) {
    const data = { activity: { ...activity, user: { ...activity.user } }, reaction: { ...hostedCodex.prReaction }, comments: null };
    mutate(data);
    assert.equal((await evaluate(fixture({ comments: data.comments ?? [claude(), data.activity], reactions: [data.reaction] }))).decision, 'waiting');
  }
});
test('PR thumbs-up persists across pushes and running activity; later and same-second concerns win', async () => {
  const reaction = { ...hostedCodex.prReaction, created_at: '2026-10-12T00:03:00Z' };
  const activity = comment('chatgpt-codex-connector[bot]', hostedCodex.runningActivity, 4);
  assert.equal((await evaluate(fixture({ comments: [claude('incomplete', 1), claude('clean', 2), activity], reactions: [reaction] }))).decision, 'approve');
  const advanced = fixture({ comments: [claude(), activity], reactions: [reaction], pr: { head: { sha: 'd'.repeat(40), repo: { full_name: repo } } } });
  assert.equal((await evaluate(advanced)).decision, 'approve');
  for (const time of [reaction.created_at, '2026-10-12T00:04:00Z']) {
    const concern = { ...comment('chatgpt-codex-connector[bot]', '[P1] A remaining concern', 3), created_at: time };
    assert.equal((await evaluate(fixture({ comments: [claude(), activity, concern], reactions: [reaction] }))).decision, 'concerns');
  }
  const concern = comment('chatgpt-codex-connector[bot]', '[P1] Earlier concern', 2);
  assert.equal((await evaluate(fixture({ comments: [claude(), activity, concern], reactions: [reaction] }))).decision, 'approve');
});
test('native thumbs-up cannot hide unresolved findings or formal change requests', async () => {
  const activity = comment('chatgpt-codex-connector[bot]', hostedCodex.automaticActivity, 2);
  for (const body of ['[P2] Significant unresolved finding', 'Unclassified unresolved feedback']) {
    const thread = { id: 'thread', isResolved: false, comments: conn([{ body, author: { login: 'chatgpt-codex-connector', __typename: 'Bot' } }]) };
    assert.equal((await evaluate(fixture({ comments: [claude(), activity], reactions: [hostedCodex.prReaction], threads: [thread] }))).decision, 'concerns');
  }
  assert.equal((await evaluate(fixture({ comments: [claude(), activity], reactions: [hostedCodex.prReaction], reviews: [review('chatgpt-codex-connector[bot]', 'CHANGES_REQUESTED')] }))).decision, 'concerns');
});
test('complete native reaction pagination and decision timestamps are required', async () => {
  const activity = comment('chatgpt-codex-connector[bot]', hostedCodex.automaticActivity, 2);
  const f = fixture({ comments: [claude(), activity], reactions: [...Array.from({ length: 100 }, () => ({ content: 'heart' })), hostedCodex.prReaction] });
  assert.equal((await evaluate(f)).decision, 'approve');
  assert.equal(f.calls.filter(c => c.route.includes('/reactions?')).length, 2);
  for (const changed of [{ ...hostedCodex.prReaction, id: null }, { ...hostedCodex.prReaction, created_at: 'bad' }]) {
    await assert.rejects(evaluate(fixture({ comments: [claude(), activity], reactions: [changed] })), /Incomplete Codex reaction/);
  }
});
test('structured Claude verdicts distinguish resolved history from current concerns', async () => {
  const resolved = claude();
  resolved.body = 'The earlier finding that should be fixed before merge is now resolved.\n' + resolved.body;
  assert.equal((await evaluate(fixture({ comments: [resolved, codex()] }))).decision, 'approve');
  const existing = { ...review(bot, 'APPROVED'), body: '<!-- gumnut-pr-approval/v1 -->' };
  const result = await publish({ ...fixture({ comments: [resolved, codex()], reviews: [existing] }), runURL,
    publisher: () => { throw Error('resolved history must not withdraw an approval'); } });
  assert.equal(result.published, false);
  assert.equal(result.decision, 'approve');
  const incomplete = claude('incomplete', 3);
  incomplete.body = 'The earlier finding that should be fixed before merge is resolved, but coverage is incomplete.\n' + incomplete.body;
  assert.equal((await evaluate(fixture({ comments: [claude(), codex(), incomplete] }))).decision, 'approve');
  const concern = claude('concerns');
  concern.body = 'The earlier finding that should be fixed before merge is resolved, but a new concern remains.\n' + concern.body;
  assert.equal((await evaluate(fixture({ comments: [concern, codex()] }))).decision, 'concerns');
});
test('significant concerns cannot be hidden by clean or incomplete wrappers', async () => {
  for (const source of [claude(), claude('incomplete'), codex()]) {
    source.body += source.user.login === 'claude[bot]' ? '\n<!-- gumnut-review-finding/v1 severity=significant -->' : '\n[P1] A remaining concern';
    assert.equal((await evaluate(fixture({ comments: [source, ...(source.user.login === 'claude[bot]' ? [codex()] : [claude()])] }))).decision, 'concerns');
  }
});
test('recognized hosted clean output accepts only its observed content and boilerplate', async () => {
  const clean = { ...codex(), body: codex().body.replace('issues.', 'issues. Keep them coming!') + '\n\n' + hostedCodex.reviewFooter };
  assert.equal((await evaluate(fixture({ comments: [claude(), clean] }))).decision, 'approve');
  assert.equal((await evaluate(fixture({ comments: [claude(), { ...codex(), body: hostedCodex.cleanVariant }] }))).decision, 'approve');
  const conflicting = { ...codex(), body: hostedCodex.cleanVariant.replace('More of your lovely PRs please.', '[P1] A significant issue remains.') };
  assert.equal((await evaluate(fixture({ comments: [claude(), conflicting] }))).decision, 'concerns');
  for (const body of [clean.body + '\nPotential data loss remains.', clean.body.replace('</details>', 'Potential data loss remains.\n</details>'), codex().body + '\nPotential data loss remains.']) {
    assert.equal((await evaluate(fixture({ comments: [claude(), { ...clean, body }] }))).decision, 'concerns');
  }
  for (const body of [hostedCodex.activity + '\nUnexpected substantive feedback', '<!-- codex-pull-request-review-summary -->\n\n## Codex Review Summary\nPotential data loss; fix before merge']) {
    const changed = comment('chatgpt-codex-connector[bot]', body, 4);
    assert.equal((await evaluate(fixture({ comments: [claude(), codex(), changed] }))).decision, 'concerns');
  }
});
test('formal P3-only Codex review signs off without treating activity or unrelated findings as approval', async () => {
  const r = { ...review('chatgpt-codex-connector[bot]', 'COMMENTED', 4), body: `### 💡 Codex Review\n\nHere are some automated review suggestions for this pull request.\n\n**Reviewed commit:** \`${old.slice(0, 7)}\`\n\n${hostedCodex.reviewFooter}` };
  const finding = (body, id = r.id) => ({ body, author: { login: 'chatgpt-codex-connector', __typename: 'Bot' }, pullRequestReview: { databaseId: id } });
  const options = comments => ({ comments, reviews: [r], threads: [{ id: 'advice', isResolved: false, comments: conn([finding('[P3] Optional improvement')]) }] });
  const result = await evaluate(fixture(options([claude()])));
  assert.equal(result.decision, 'approve');
  assert.equal(result.signoffs.codex.kind, 'advisory-review');
  assert.equal(result.signoffs.codex.commit, old);
  for (const body of ['[P2] Significant improvement', 'Unknown severity or format', 'Unclassified consequential issue in P3 storage']) {
    const f = options([claude(), codex()]);f.threads[0].comments.nodes.push(finding(body));
    assert.equal((await evaluate(fixture(f))).decision, 'concerns');
  }
  const unrelated = options([claude(), codex()]);unrelated.threads[0].comments.nodes = [finding('[P3] Advice from another review', 5)];
  assert.equal((await evaluate(fixture(unrelated))).decision, 'concerns');
  const unknownBody = options([claude(), codex()]);unknownBody.reviews = [{ ...r, body: r.body.replace('**Reviewed commit:**', 'An unclassified consequential concern remains.\n\n**Reviewed commit:**') }];
  assert.equal((await evaluate(fixture(unknownBody))).decision, 'concerns');
  const wrongCommit = options([claude(), codex()]);wrongCommit.reviews = [{ ...r, body: r.body.replace(old.slice(0, 7), head.slice(0, 7)) }];
  assert.equal((await evaluate(fixture(wrongCommit))).decision, 'concerns');
  const badge = options([claude()]);badge.threads[0].comments.nodes = [finding('**<sub><sub>![P3 Badge](https://img.shields.io/badge/P3-blue?style=flat)</sub></sub> Optional improvement**')];
  assert.equal((await evaluate(fixture(badge))).decision, 'approve');
});
test('formal approval bodies cannot conceal significant or unknown feedback', async () => {
  for (const login of ['claude[bot]', 'chatgpt-codex-connector[bot]']) {
    for (const body of ['A consequential regression remains.', login === 'claude[bot]' ? '<!-- gumnut-review-finding/v1 severity=significant -->' : '[P1] data loss']) {
      const approved = { ...review(login, 'APPROVED', 4), body };
      assert.equal((await evaluate(fixture({ reviews: [approved] }))).decision, 'concerns');
    }
  }
});
test('unknown unresolved reviewer threads block even after a later clean decision', async () => {
  for (const login of ['claude', 'chatgpt-codex-connector']) {
    const thread = { id: 'unknown', isResolved: false, comments: conn([{ body: 'Unrecognized severity: this can corrupt data.', author: { login, __typename: 'Bot' } }]) };
    assert.equal((await evaluate(fixture({ threads: [thread] }))).decision, 'concerns');
    thread.isResolved = true;
    assert.equal((await evaluate(fixture({ threads: [thread] }))).decision, 'approve');
  }
  const advisory = { id: 'advisory', isResolved: false, comments: conn([{ body: '**Claude review — optional improvement (advisory)**\nUseful but optional.', author: { login: 'claude', __typename: 'Bot' } }]) };
  assert.equal((await evaluate(fixture({ threads: [advisory] }))).decision, 'approve');
});
test('source identities require both exact login and Bot type', async () => {
  const fake = claude(); fake.user.type = 'User';
  assert.equal((await evaluate(fixture({ comments: [fake, codex()] }))).decision, 'waiting');
});
test('formal source approvals count and dismissed approval does not', async () => {
  assert.equal((await evaluate(fixture({ comments: [], reviews: [review('claude[bot]', 'APPROVED'), review('chatgpt-codex-connector[bot]', 'APPROVED')] }))).decision, 'approve');
  assert.equal((await evaluate(fixture({ comments: [codex()], reviews: [review('claude[bot]', 'DISMISSED')] }))).decision, 'waiting');
});
test('newer source dismissal clears old signoff until a later clean declaration', async () => {
  for (const login of ['claude[bot]', 'chatgpt-codex-connector[bot]']) {
    const reviews = [review(login, 'APPROVED', 3), review(login, 'DISMISSED', 4)];
    assert.equal((await evaluate(fixture({ reviews }))).decision, 'waiting');
    const clean = login === 'claude[bot]' ? claude('clean', 5) : { ...codex(), id: 5, created_at: '2026-10-12T00:05:00Z' };
    assert.equal((await evaluate(fixture({ reviews, comments: [claude(), codex(), clean] }))).decision, 'approve');
    assert.equal((await evaluate(fixture({ reviews: [...reviews, review(login, 'APPROVED', 5)] }))).decision, 'approve');
  }
});
test('latest human changes request blocks until approved or dismissed; unknown bots ignored', async () => {
  const human = { ...review('alice', 'CHANGES_REQUESTED'), user: { login: 'alice', type: 'User' } };
  assert.equal((await evaluate(fixture({ reviews: [human] }))).decision, 'concerns');
  for (const state of ['APPROVED', 'DISMISSED']) assert.equal((await evaluate(fixture({ reviews: [human, { ...human, id: 4, submitted_at: '2026-10-12T01:00:00Z', state }] }))).decision, 'approve');
  assert.equal((await evaluate(fixture({ reviews: [review('unknown[bot]', 'CHANGES_REQUESTED')] }))).decision, 'approve');
});
test('significant unresolved replies block even outdated threads; resolved thread clears', async () => {
  for (const [login, body] of [['claude[bot]', '<!-- gumnut-review-finding/v1 severity=significant -->'], ['claude[bot]', 'This should be fixed before merge'], ['chatgpt-codex-connector[bot]', '**[P2] Fix this**']]) {
    const thread = { id: 't1', isResolved: false, isOutdated: true, comments: conn([{ body: 'initial', author: { login: 'alice', __typename: 'User' } }, { body, author: { login: login.replace('[bot]', ''), __typename: 'Bot' } }]) };
    assert.equal((await evaluate(fixture({ threads: [thread] }))).decision, 'concerns');
    thread.isResolved = true;
    assert.equal((await evaluate(fixture({ threads: [thread] }))).decision, 'approve');
  }
});
test('every nested GraphQL comments page is inspected', async () => {
  const thread = { id: 't1', isResolved: false, comments: conn([], true, 'next') };
  const f = fixture({ threads: [thread], override: (_m, route, body) => route === '/graphql' && body.variables.id ? { data: { node: { comments: conn([{ body: '[P1] issue', author: { login: 'chatgpt-codex-connector', __typename: 'Bot' } }]) } } } : undefined });
  assert.equal((await evaluate(f)).decision, 'concerns');
});
test('all thread and REST pages are fetched', async () => {
  const many = Array.from({ length: 100 }, (_, i) => comment('someone', '', i + 1));
  const f = fixture({ comments: [...many, claude(), codex()], override: (_m, route, body) => route === '/graphql' && body.query.includes('reviewThreads(first:100') ? { data: { repository: { pullRequest: { reviewThreads: body.variables.cursor ? conn([]) : conn([], true, 'next') } } } } : undefined });
  assert.equal((await evaluate(f)).decision, 'approve');
  assert(f.calls.some(c => c.route.includes('/comments?') && c.route.includes('page=2')));
  assert.equal(f.calls.filter(c => c.route === '/graphql' && c.body.query.includes('reviewThreads(first:100')).length, 2);
});
test('GraphQL lastEditedAt makes an older body-only concern supersede a newer clean', async () => {
  for (const [login, body] of [['claude[bot]', 'This should be fixed before merge'], ['chatgpt-codex-connector[bot]', '[P2] fix this']]) {
    const finding = { ...review(login, 'COMMENTED', 1), body };
    const metadata = { ...graphReview(finding), lastEditedAt: '2026-10-12T00:10:00Z', updatedAt: '2026-10-12T00:10:00Z' };
    assert.equal((await evaluate(fixture({ reviews: [finding], graphReviews: [metadata] }))).decision, 'concerns');
    const clean = login === 'claude[bot]' ? claude('clean', 11) : { ...codex(), id: 11, updated_at: '2026-10-12T00:11:00Z' };
    assert.equal((await evaluate(fixture({ reviews: [finding], graphReviews: [metadata], comments: [claude(), codex(), clean] }))).decision, 'approve');
  }
});
test('GraphQL updatedAt places explicit dismissal after newer-issued clean signoff', async () => {
  const dismissed = review('claude[bot]', 'DISMISSED', 1);
  const metadata = { ...graphReview(dismissed), updatedAt: '2026-10-12T00:10:00Z' };
  assert.equal((await evaluate(fixture({ reviews: [dismissed], graphReviews: [metadata], comments: [claude('clean', 4), codex()] }))).decision, 'waiting');
  assert.equal((await evaluate(fixture({ reviews: [dismissed], graphReviews: [metadata], comments: [claude('clean', 11), codex()] }))).decision, 'approve');
});
test('source review metadata must match exact REST snapshot and cover every source review', async () => {
  const source = review('claude[bot]', 'APPROVED');
  for (const graphReviews of [[], [{ ...graphReview(source), updatedAt: null }], [{ ...graphReview(source), lastEditedAt: 'not a date' }], [{ ...graphReview(source), state: 'COMMENTED' }], [{ ...graphReview(source), body: 'different snapshot' }], [{ ...graphReview(source), author: { login: 'claude', __typename: 'User' } }], [{ ...graphReview(source), commit: { oid: head } }]]) await assert.rejects(evaluate(fixture({ reviews: [source], graphReviews })), /metadata|timestamps/);
});
test('all GraphQL review metadata pages are joined by databaseId', async () => {
  const source = review('claude[bot]', 'APPROVED', 101);
  source.submitted_at = '2026-10-12T00:01:00Z';
  const filler = Array.from({ length: 100 }, (_, i) => ({ ...graphReview(review('other[bot]', 'COMMENTED', 1)), databaseId: i + 1 }));
  const f = fixture({ comments: [codex()], reviews: [source], override: (_m, route, body) => route === '/graphql' && body.query.includes('reviews(first:100') ? { data: { repository: { pullRequest: { reviews: body.variables.cursor ? conn([graphReview(source)]) : conn(filler, true, 'reviews-next') } } } } : undefined });
  assert.equal((await evaluate(f)).decision, 'approve');
  assert.equal(f.calls.filter(c => c.route === '/graphql' && c.body.query.includes('reviews(first:100')).length, 2);
});
test('truncation, GraphQL errors, and missing collections fail closed', async () => {
  for (const override of [(_m, route) => route.includes('/check-runs?') ? { total_count: 101, check_runs: [] } : undefined, (_m, route) => route === '/graphql' ? { errors: [{ message: 'error' }] } : undefined, (_m, route) => route === '/graphql' ? { data: { repository: null } } : undefined]) await assert.rejects(evaluate(fixture({ override })));
});
test('CI must exist and pass; name collisions do not exempt checks', async () => {
  assert.equal((await evaluate(fixture({ checks: [], statuses: [] }))).decision, 'waiting');
  for (const conclusion of ['failure', 'cancelled', null]) assert.equal((await evaluate(fixture({ checks: [{ name: 'Tests', status: 'completed', conclusion }] }))).decision, 'waiting');
  for (const conclusion of ['neutral', 'skipped']) assert.equal((await evaluate(fixture({ checks: [{ name: 'Tests', status: 'completed', conclusion }] }))).decision, 'approve');
  assert.equal((await evaluate(fixture({ checks: [{ name: 'Approval assessment', status: 'completed', conclusion: 'failure' }], statuses: [{ state: 'success' }] }))).decision, 'waiting');
  assert.equal((await evaluate(fixture({ statuses: [{ state: 'pending' }] }))).decision, 'waiting');
});
test('observed active or failed current-head workflow runs block with only one passing check', async () => {
  for (const run of [{ status: 'queued', conclusion: null }, { status: 'in_progress', conclusion: null }, { status: 'completed', conclusion: 'failure' }, { status: 'completed', conclusion: 'cancelled' }]) assert.equal((await evaluate(fixture({ runs: [run] }))).decision, 'waiting');
  assert.equal((await evaluate(fixture({ runs: [{ status: 'completed', conclusion: 'success' }] }))).decision, 'approve');
  const f = fixture({ runs: [...Array.from({ length: 100 }, () => ({ status: 'completed', conclusion: 'success' })), { status: 'in_progress', conclusion: null }] });
  assert.equal((await evaluate(f)).decision, 'waiting');
  assert(f.calls.some(c => c.route.includes(`/actions/runs?head_sha=${head}`) && c.route.includes('page=2')));
});
test('latest workflow disposition clears historical failure or pending run on the same head', async () => {
  for (const status of ['completed', 'in_progress']) {
    const earlier = workflowRun({ id: 1, status, conclusion: status === 'completed' ? 'failure' : null });
    const replacement = workflowRun({ id: 2, created_at: '2026-10-12T02:00:00Z' });
    assert.equal((await evaluate(fixture({ runs: [replacement, earlier] }))).decision, 'approve');
  }
  assert.equal((await evaluate(fixture({ runs: [workflowRun({ id: 1 }), workflowRun({ id: 2, created_at: '2026-10-12T02:00:00Z', status: 'in_progress', conclusion: null })] }))).decision, 'waiting');
  assert.equal((await evaluate(fixture({ runs: [workflowRun({ workflow_id: 10 }), workflowRun({ id: 2, workflow_id: 11, conclusion: 'failure' })] }))).decision, 'waiting');
});
test('latest run attempt on one workflow run ID controls retry disposition', async () => {
  const prior = workflowRun({ conclusion: 'failure', run_attempt: 1 });
  const retry = workflowRun({ conclusion: 'success', run_attempt: 2 });
  assert.equal((await evaluate(fixture({ runs: [retry, prior] }))).decision, 'approve');
  assert.equal((await evaluate(fixture({ runs: [prior, { ...retry, status: 'in_progress', conclusion: null }] }))).decision, 'waiting');
  await assert.rejects(evaluate(fixture({ runs: [workflowRun({ workflow_id: null })] })), /metadata/);
  await assert.rejects(evaluate(fixture({ runs: [workflowRun({ head_sha: old })] })), /metadata/);
});
test('closed, draft, fork, nondefault and self-authored PRs ineligible', async () => {
  for (const pr of [{ state: 'closed' }, { draft: true }, { user: { login: bot } }, { head: { sha: head, repo: { full_name: 'fork/repo' } } }, { base: { sha: base, ref: 'other', repo: { full_name: repo } } }]) assert.equal((await evaluate(fixture({ pr }))).decision, 'ineligible');
});
test('invalid input rejects before API access', async () => {
  for (const values of [{ number: 0 }, { number: '1/2' }, { repo: '../repo' }, { bot: 'alice' }]) await assert.rejects(evaluate({ ...fixture(), ...values }));
});
function publisherFixture(options = {}) {
  const calls = [];
  let pending;
  const publisher = async (method, route, body) => {
    calls.push({ method, route, body });
    if (method === 'GET') return options.pending ?? [];
    if (route.endsWith('/events')) {
      assert(pending, 'submission requires a previously created pending review');
      if (options.failSubmit) throw new Error('uncertain submit');
      return { ...pending, state: body.event === 'APPROVE' ? 'APPROVED' : 'CHANGES_REQUESTED' };
    }
    if (options.failCreate) throw new Error('uncertain create');
    assert.equal(body.event, undefined, 'creating pending intent must not submit');
    pending = { id: 42, user: user(bot), state: 'PENDING', commit_id: body.commit_id, body: body.body };
    return pending;
  };
  return { publisher, calls, get pending() { return pending; } };
}
test('reaction approval explains its PR-level scope without inventing a reviewed commit', async () => {
  const p = publisherFixture();
  const activity = comment('chatgpt-codex-connector[bot]', hostedCodex.automaticActivity, 2);
  const f = fixture({ comments: [claude(), activity], reactions: [hostedCodex.prReaction], override: (_m, route) => route.endsWith('/reviews/42') ? { ...p.pending, state: 'APPROVED', html_url: 'review-url' } : undefined });
  const result = await publish({ ...f, runURL, publisher: p.publisher });
  assert.equal(result.published, true);
  assert.match(p.pending.body, /codex: https:\/\/github\.com\/example\/repo\/pull\/7 \(reviewed commit not reported; PR-level 👍 reaction 563501689;/);
  assert.doesNotMatch(p.pending.body, /reviewed commit null/);
});
test('publish creates durable intent, submits once, readbacks, and does not repeat on push', async () => {
  let written;
  const f = fixture({ override: (_m, route) => route.endsWith('/reviews/42') ? { id: 42, user: user(bot), state: 'APPROVED', commit_id: head, body: written.body, html_url: 'review-url' } : undefined });
  const p = publisherFixture();
  const result = await publish({ ...f, runURL, publisher: async (...args) => { const response = await p.publisher(...args); if (args[0] === 'POST' && !args[1].endsWith('/events')) written = args[2]; return response; } });
  assert.equal(result.published, true);
  assert.equal(written.commit_id, head);
  assert(written.body.includes(old));
  assert(written.body.includes(`Current-head CI passed for ${head}: https://github.com/${repo}/pull/7/checks`));
  assert(written.body.includes('Observed 1 checks, 0 statuses, and 0 latest workflow runs.'));
  assert(written.body.includes('Sign-offs may predate the current commit'));
  assert(written.body.includes('did not independently review the code'));
  assert(written.body.includes('bot identity does not attest review workflow origin'));
  assert.deepEqual(p.calls.map(c => [c.method, c.route.split('?')[0]]), [['GET', `/repos/${repo}/pulls/7/reviews`], ['POST', `/repos/${repo}/pulls/7/reviews`], ['POST', `/repos/${repo}/pulls/7/reviews/42/events`]]);
  const existing = { ...review(bot, 'APPROVED'), body: '<!-- gumnut-pr-approval/v1 -->' };
  assert.equal((await publish({ ...fixture({ reviews: [existing] }), runURL, publisher: () => { throw Error('must not write'); } })).published, false);
});
test('concerns withdraw prior coordinator approval only; clean after withdrawal reapproves', async () => {
  for (const prior of [null, 'APPROVED', 'CHANGES_REQUESTED']) {
    const existing = prior ? [{ ...review(bot, prior), body: '<!-- gumnut-pr-approval/v1 -->' }] : [];
    let written;
    const f = fixture({ comments: [claude('concerns'), codex()], reviews: existing, override: (_m, route) => route.endsWith('/reviews/42') ? { user: user(bot), state: 'CHANGES_REQUESTED', commit_id: head, body: written.body } : undefined });
    const p = publisherFixture();
    const result = await publish({ ...f, runURL, publisher: async (...args) => { const response = await p.publisher(...args); if (args[0] === 'POST' && !args[1].endsWith('/events')) written = args[2]; return response; } });
    assert.equal(result.published, prior === 'APPROVED');
    if (written) assert.equal(p.calls.at(-1).body.event, 'REQUEST_CHANGES');
  }
  const f = fixture({ reviews: [{ ...review(bot, 'CHANGES_REQUESTED'), body: '<!-- gumnut-pr-approval/v1 -->' }] });
  const p = publisherFixture({ failSubmit: true });
  await assert.rejects(publish({ ...f, runURL, publisher: p.publisher }), /uncertain/);
  assert.equal(p.calls.at(-1).body.event, 'APPROVE');
});
test('unrecognized feedback withholds an existing approval with a clear explanation', async () => {
  const existing = { ...review(bot, 'APPROVED'), body: '<!-- gumnut-pr-approval/v1 -->' };
  const unknown = comment('claude[bot]', 'Changed verdict format with a new unresolved concern', 4);
  const p = publisherFixture({ failSubmit: true });
  await assert.rejects(publish({ ...fixture({ comments: [claude(), codex(), unknown], reviews: [existing] }), runURL, publisher: p.publisher }), /uncertain submit/);
  assert.equal(p.calls.at(-1).body.event, 'REQUEST_CHANGES');
  assert(p.pending.body.includes('Unrecognized reviewer feedback requires clarification'));
});
test('uncertain writes and mismatching readback are not retried', async () => {
  for (const options of [{ failCreate: true }, { failSubmit: true }]) {
    const p = publisherFixture(options);
    await assert.rejects(publish({ ...fixture(), runURL, publisher: p.publisher }), /uncertain/);
    assert.equal(p.calls.filter(c => c.method === 'POST' && !c.route.endsWith('/events')).length, 1);
    assert.equal(p.calls.filter(c => c.route.endsWith('/events')).length, options.failSubmit ? 1 : 0);
  }
  const p = publisherFixture();
  await assert.rejects(publish({ ...fixture({ override: (_m, route) => route.endsWith('/reviews/42') ? { user: user('imposter[bot]') } : undefined }), runURL, publisher: p.publisher }), /readback/);
});
test('pending prior intent blocks new writes and requires operator inspection across pushes', async () => {
  const pending = { ...review(bot, 'PENDING'), body: '<!-- gumnut-pr-approval/v1 -->' };
  const p = publisherFixture({ pending: [...Array.from({ length: 100 }, () => review('other[bot]', 'COMMENTED')), pending] });
  // Paginate with the publisher identity, which sees its private pending intent.
  const publisher = async (method, route, body) => {
    if (method === 'GET') {
      const page = Number(new URL(route, 'https://api.github.com').searchParams.get('page'));
      p.calls.push({ method, route });
      return page === 1 ? Array.from({ length: 100 }, () => review('other[bot]', 'COMMENTED')) : [pending];
    }
    return p.publisher(method, route, body);
  };
  await assert.rejects(publish({ ...fixture(), runURL, publisher }), /operator inspection/);
  assert.equal(p.calls.length, 2);
  assert(p.calls.every(c => c.method === 'GET'));
  assert.equal((await evaluate(fixture({ comments: [codex()], reviews: [{ ...review('claude[bot]', 'PENDING') }] }))).decision, 'waiting');
});
test('App-visible submitted review suppresses duplicate writes when reader lags', async () => {
  for (const state of ['APPROVED', 'CHANGES_REQUESTED']) {
    const standing = { ...review(bot, state), body: '<!-- gumnut-pr-approval/v1 -->' };
    const options = state === 'APPROVED' ? {} : { comments: [claude('concerns'), codex()], reviews: [{ ...review(bot, 'APPROVED'), body: '<!-- gumnut-pr-approval/v1 -->' }] };
    const p = publisherFixture({ pending: [standing] });
    assert.equal((await publish({ ...fixture(options), runURL, publisher: p.publisher })).published, false);
    assert.equal(p.calls.filter(c => c.method === 'POST').length, 0);
  }
});
test('explicit coordinator dismissal persists across pushes until a later clean signoff', async () => {
  const dismissed = { ...review(bot, 'DISMISSED'), body: '<!-- gumnut-pr-approval/v1 -->' };
  const metadata = { ...graphReview(dismissed), updatedAt: '2026-10-12T00:10:00Z' };
  const options = { reviews: [dismissed], graphReviews: [metadata] };
  for (const sha of [head, 'd'.repeat(40)]) {
    const f = fixture({ ...options, pr: { head: { sha, repo: { full_name: repo } } } });
    assert.equal((await evaluate(f)).decision, 'waiting');
    assert.equal((await publish({ ...f, runURL, publisher: () => { throw Error('must not write'); } })).published, false);
  }
  const sameSecond = { ...claude('clean', 11), updated_at: metadata.updatedAt };
  assert.equal((await evaluate(fixture({ ...options, comments: [sameSecond, codex()] }))).decision, 'waiting');
  const later = claude('clean', 11);
  const f = fixture({ ...options, comments: [later, codex()] });
  assert.equal((await evaluate(f)).decision, 'approve');
  const p = publisherFixture({ pending: [dismissed], failSubmit: true });
  await assert.rejects(publish({ ...f, runURL, publisher: p.publisher }), /uncertain submit/);
  assert.equal(p.calls.at(-1).body.event, 'APPROVE');
});
test('coordinator dismissal needs complete matching metadata and fresh publisher visibility', async () => {
  const dismissed = { ...review(bot, 'DISMISSED'), body: '<!-- gumnut-pr-approval/v1 -->' };
  for (const graphReviews of [[], [{ ...graphReview(dismissed), state: 'APPROVED' }]]) await assert.rejects(evaluate(fixture({ reviews: [dismissed], graphReviews })), /metadata/);
  for (const reviews of [[], [{ ...dismissed, state: 'CHANGES_REQUESTED' }]]) {
    const p = publisherFixture({ pending: [dismissed] });
    const result = await publish({ ...fixture({ reviews }), runURL, publisher: p.publisher });
    assert.equal(result.published, false);
    assert.equal(result.decision, 'waiting');
    assert.equal(p.calls.filter(c => c.method === 'POST').length, 0);
  }
});
test('publisher checks older unobserved dismissals beneath newer changes requests before approving', async () => {
  const dismissed = { ...review(bot, 'DISMISSED', 3), body: '<!-- gumnut-pr-approval/v1 -->' };
  const newer = { ...review(bot, 'CHANGES_REQUESTED', 4), body: '<!-- gumnut-pr-approval/v1 -->' };
  const p = publisherFixture({ pending: [dismissed, newer] });
  const result = await publish({ ...fixture({ reviews: [newer] }), runURL, publisher: p.publisher });
  assert.deepEqual(result.observedDismissalIds, []);
  assert.equal(result.decision, 'waiting');
  assert.equal(result.published, false);
  assert.match(result.reason, /dismissal absent/);
  assert(p.calls.every(c => c.method === 'GET'));
});
test('newer coordinator changes request cannot clear an older dismissal without later clean evidence', async () => {
  const dismissed = { ...review(bot, 'DISMISSED', 3), body: '<!-- gumnut-pr-approval/v1 -->' };
  const newer = { ...review(bot, 'CHANGES_REQUESTED', 11), body: '<!-- gumnut-pr-approval/v1 -->' };
  const metadata = [{ ...graphReview(dismissed), updatedAt: '2026-10-12T00:10:00Z' }, graphReview(newer)];
  const f = fixture({ reviews: [dismissed, newer], graphReviews: metadata });
  const result = await evaluate(f);
  assert.equal(result.existingApproval.state, 'CHANGES_REQUESTED');
  assert.deepEqual(result.observedDismissalIds, [3]);
  assert.equal(result.decision, 'waiting');
  assert.match(result.reason, /dismissed/);
  assert.equal((await publish({ ...f, runURL, publisher: () => { throw Error('must not access publisher'); } })).published, false);
  const sameSecond = { ...claude('clean', 12), updated_at: '2026-10-12T00:10:00Z' };
  assert.equal((await evaluate(fixture({ reviews: [dismissed, newer], graphReviews: metadata, comments: [sameSecond, codex()] }))).decision, 'waiting');
  const later = fixture({ reviews: [dismissed, newer], graphReviews: metadata, comments: [claude('clean', 12), codex()] });
  assert.equal((await evaluate(later)).decision, 'approve');
  const p = publisherFixture({ pending: [dismissed, newer], failSubmit: true });
  await assert.rejects(publish({ ...later, runURL, publisher: p.publisher }), /uncertain submit/);
  assert.equal(p.calls.at(-1).body.event, 'APPROVE');
});
test('latest marked dismissal controls timing across all own reviews', async () => {
  const first = { ...review(bot, 'DISMISSED', 3), body: '<!-- gumnut-pr-approval/v1 -->' };
  const second = { ...review(bot, 'DISMISSED', 4), body: '<!-- gumnut-pr-approval/v1 -->' };
  const newer = { ...review(bot, 'CHANGES_REQUESTED', 13), body: '<!-- gumnut-pr-approval/v1 -->' };
  const reviews = [first, second, newer];
  const graphReviews = [{ ...graphReview(first), updatedAt: '2026-10-12T00:10:00Z' }, { ...graphReview(second), updatedAt: '2026-10-12T00:12:00Z' }, graphReview(newer)];
  assert.equal((await evaluate(fixture({ reviews, graphReviews, comments: [claude('clean', 11), codex()] }))).decision, 'waiting');
  assert.equal((await evaluate(fixture({ reviews, graphReviews, comments: [claude('clean', 14), codex()] }))).decision, 'approve');
});
test('every authenticated marked dismissal is recorded, and later clean permits fully observed approval', async () => {
  const marked = id => ({ ...review(bot, 'DISMISSED', id), body: '<!-- gumnut-pr-approval/v1 -->' });
  const dismissed = [marked(3), marked(4)];
  const newer = { ...review(bot, 'CHANGES_REQUESTED', 5), body: '<!-- gumnut-pr-approval/v1 -->' };
  const unrelated = [{ ...marked(6), body: 'unmarked review' }, { ...marked(7), user: user('another[bot]') }, { ...marked(8), user: { login: bot, type: 'User' } }];
  const f = fixture({ reviews: [...dismissed, newer, ...unrelated], comments: [claude('clean', 11), codex()] });
  const result = await evaluate(f);
  assert.deepEqual(result.observedDismissalIds, [3, 4]);
  assert.equal(result.decision, 'approve');
  const p = publisherFixture({ pending: [...dismissed, newer], failSubmit: true });
  await assert.rejects(publish({ ...f, runURL, publisher: p.publisher }), /uncertain submit/);
  assert.equal(p.calls.at(-1).body.event, 'APPROVE');
});
test('one unseen dismissal among observed dismissals blocks approval', async () => {
  const dismissed = id => ({ ...review(bot, 'DISMISSED', id), body: '<!-- gumnut-pr-approval/v1 -->' });
  const newer = { ...review(bot, 'CHANGES_REQUESTED', 5), body: '<!-- gumnut-pr-approval/v1 -->' };
  const p = publisherFixture({ pending: [dismissed(3), dismissed(4), newer] });
  const result = await publish({ ...fixture({ reviews: [dismissed(3), newer], comments: [claude('clean', 11), codex()] }), runURL, publisher: p.publisher });
  assert.deepEqual(result.observedDismissalIds, [3]);
  assert.equal(result.decision, 'waiting');
  assert(p.calls.every(c => c.method === 'GET'));
});
test('publisher-only dismissal does not block withholding approval through REQUEST_CHANGES', async () => {
  const prior = { ...review(bot, 'APPROVED', 4), body: '<!-- gumnut-pr-approval/v1 -->' };
  const unseen = { ...review(bot, 'DISMISSED', 3), body: '<!-- gumnut-pr-approval/v1 -->' };
  const p = publisherFixture({ pending: [unseen], failSubmit: true });
  await assert.rejects(publish({ ...fixture({ reviews: [prior], comments: [claude('concerns', 11), codex()] }), runURL, publisher: p.publisher }), /uncertain submit/);
  assert.equal(p.calls.at(-1).body.event, 'REQUEST_CHANGES');
});
test('exported pagination helper returns all pages and preserves truncation rejection', async () => {
  const items = Array.from({ length: 101 }, (_, id) => ({ id }));
  const api = async (_method, route) => {
    const page = Number(new URL(route, 'https://api.github.com').searchParams.get('page'));
    return { total_count: items.length, workflow_runs: items.slice((page - 1) * 100, page * 100) };
  };
  assert.deepEqual(await pages(api, `/repos/${repo}/actions/runs`, 'workflow_runs'), items);
  await assert.rejects(pages(async () => ({ total_count: 2, workflow_runs: [] }), `/repos/${repo}/actions/runs`, 'workflow_runs'), /Truncated/);
});
test('createApi rejects external hosts and invalid routes before requests', async () => {
  const api = createApi('secret');
  for (const route of ['https://evil.example/repos/a/b', '//evil.example/repos/a/b', '/users/me', '/repos/a/b/../../users/me']) await assert.rejects(api('GET', route), /route/);
});
