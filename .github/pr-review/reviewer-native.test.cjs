// Contract tests exercise the exact native collector and publication handlers.
// Set GH_AW_ACTIONS_DIR to actions/setup/js at the workflow's pinned gh-aw commit.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
process.env.PR_REVIEW_BOT_LOGIN = 'gumnut-reviewer[bot]';
const { preparePublication, coverageDeclaration, marker, verifyDelivery, admit } = require('./reviewer.cjs');
const runtime = process.env.GH_AW_ACTIONS_DIR;
if (!runtime) throw new Error('GH_AW_ACTIONS_DIR must identify the pinned native runtime');
const lock = fs.readFileSync(path.join(__dirname, '../workflows/pr-review.lock.yml'), 'utf8');
const pin = lock.match(/uses: github\/gh-aw\/actions\/setup@([0-9a-f]{40})/)[1];
assert.equal(require('node:child_process').execFileSync('git', ['-C', runtime, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(), pin);
const head = 'a'.repeat(40), bot = { login: process.env.PR_REVIEW_BOT_LOGIN, type: 'Bot' };
const context = { eventName: 'pull_request_target', actor: 'example', repo: { owner: 'example', repo: 'repo' }, runId: 123, serverUrl: 'https://github.com', payload: { action: 'opened', pull_request: { number: 7 }, repository: { default_branch: 'main' } } };
const pr = { number: 7, state: 'open', draft: false, head: { sha: head }, base: { sha: 'b'.repeat(40) }, user: { login: 'example' } };
const binding = { number: 7, head, author: 'example' };
const validation = JSON.parse(lock.match(/GH_AW_VALIDATION_JSON: \|\n((?: {12}.*\n)+)/)[1].split('\n').map(line => line.slice(12)).join('\n'));
const config = JSON.parse(JSON.parse(lock.match(/GH_AW_SAFE_OUTPUTS_CONFIG: (.+)/)[1]));
for (const value of Object.values(config)) {
  if (value.commit_id) value.commit_id = head;
  if (value.target) value.target = '7';
}
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'reviewer-native-'));
process.env.GH_AW_SAFE_OUTPUTS_CONFIG_PATH = path.join(scratch, 'config.json');
process.env.GH_AW_VALIDATION_CONFIG_PATH = path.join(scratch, 'validation.json');
process.env.GH_AW_SAFE_OUTPUTS = path.join(scratch, 'output.ndjson');
process.env.GH_AW_ALLOWED_DOMAINS = JSON.parse(lock.match(/GH_AW_ALLOWED_DOMAINS: (.+)/)[1]);
process.env.GITHUB_SERVER_URL = 'https://github.com';
process.env.GITHUB_API_URL = 'https://api.github.com';
process.env.GITHUB_REPOSITORY = 'example/repo';
fs.writeFileSync(process.env.GH_AW_SAFE_OUTPUTS_CONFIG_PATH, JSON.stringify(config));
fs.writeFileSync(process.env.GH_AW_VALIDATION_CONFIG_PATH, JSON.stringify(validation));
let collected, published, comments;
global.core = { info() {}, debug() {}, warning() {}, error() {}, exportVariable() {}, setOutput(key, value) { if (key === 'output') collected = JSON.parse(value); }, summary: { addRaw() { return this; }, async write() {} } };
global.context = context;
global.github = {
  rest: {
    pulls: {
      get: async () => ({ data: pr }),
      listFiles: async () => ({ data: [{ filename: 'source.cjs' }] }),
      listReviews: async () => ({ data: published ? [published] : [] }),
      listCommentsForReview: async () => ({ data: comments || [] }),
      createReview: async args => {
        published = { id: 19, user: bot, commit_id: args.commit_id, state: args.event === 'APPROVE' ? 'APPROVED' : 'COMMENTED', body: args.body, html_url: 'review-url' };
        comments = (args.comments || []).map(comment => ({ ...comment, user: bot, original_line: comment.line }));
        return { data: published };
      },
    },
  },
  paginate: async (route, args) => (await route(args)).data,
};
const native = name => require(path.join(runtime, name));
const sanitize = body => native('sanitize_content.cjs').sanitizeContent(body, { allowedAliases: [], maxMentions: 50 });
async function collect(items) {
  collected = undefined; published = undefined; comments = [];
  fs.writeFileSync(process.env.GH_AW_SAFE_OUTPUTS, items.map(item => JSON.stringify(item)).join('\n'));
  await native('collect_ndjson_output.cjs').main();
  assert.ok(collected, 'native collector must return validated output');
  return collected;
}
const submit = coverage => ({ type: 'submit_pull_request_review', event: coverage === 'complete' ? 'APPROVE' : 'COMMENT', body: 'All applicable review lanes assessed.\n' + coverageDeclaration(head, coverage) });
const finding = { type: 'create_pull_request_review_comment', path: 'source.cjs', line: 12, body: 'Finding with @someone https://example.net/path <!-- untrusted -->' };
async function publish(output) {
  const buffer = native('pr_review_buffer.cjs').createReviewBuffer();
  const inline = await native('create_pr_review_comment.cjs').main({ ...config.create_pull_request_review_comment, _prReviewBuffer: buffer });
  const formal = await native('submit_pr_review.cjs').main({ ...config.submit_pull_request_review, _prReviewBuffer: buffer });
  for (const item of output.items) assert.equal((await (item.type === finding.type ? inline : formal)(item, {})).success, true);
  assert.equal((await buffer.submitReview()).success, true);
}
const verify = () => verifyDelivery({ github: global.github, context, ...binding, agent: 'success', detection: 'success', publisher: 'success' });
test('native ingestion, guard, publisher and readback retain trusted coverage and exact finding delivery', async () => {
  const output = await collect([finding, { ...submit('complete'), body: submit('complete').body + '\n' + marker(head, 'complete') }]);
  assert.deepEqual(output.errors, []);
  assert.equal(output.items[1].body.includes(marker(head, 'complete')), false, 'collector strips raw HTML markers');
  await preparePublication({ github: global.github, context, output, binding, sanitize });
  await publish(output);
  assert.ok(published.body.includes(marker(head, 'complete')), 'native publisher preserves trusted marker');
  assert.equal(await verify(), 'review-url');
  assert.equal((await admit({ github: global.github, context, enabled: 'true' })).reason, 'already-reviewed-current-head');
  comments = [];
  await assert.rejects(verify());
  assert.equal((await admit({ github: global.github, context, enabled: 'true' })).eligible, 'true');
});
test('native rejected inline item makes otherwise complete formal output fail closed', async () => {
  const output = await collect([{ ...finding, line: -1 }, submit('complete')]);
  assert.equal(output.items.length, 1, 'native collector drops invalid inline declaration');
  assert.ok(output.errors.length > 0, 'native collector retains rejection evidence');
  await assert.rejects(preparePublication({ github: global.github, context, output, binding, sanitize }), /collector rejected/);
  assert.equal(published, undefined, 'guard never invokes native publication after rejected declaration');
});
test('native body-only clean and limitation reviews preserve their distinct completion states', async () => {
  for (const coverage of ['complete', 'incomplete']) {
    const output = await collect([submit(coverage)]);
    await preparePublication({ github: global.github, context, output, binding, sanitize });
    await publish(output);
    if (coverage === 'complete') assert.equal(await verify(), 'review-url');
    else await assert.rejects(verify(), /coverage remains incomplete/);
  }
});
test('native complete blocking COMMENT remains complete but own-author coverage fails closed', async () => {
  const output = await collect([{ ...finding, body: '🔴 blocking: supported finding' }, { ...submit('complete'), event: 'COMMENT' }]);
  await preparePublication({ github: global.github, context, output, binding, sanitize });
  await publish(output);
  assert.equal(published.state, 'COMMENTED');
  assert.equal(await verify(), 'review-url');
  assert.equal((await admit({ github: global.github, context, enabled: 'true' })).reason, 'already-reviewed-current-head');
  const own = await collect([submit('complete')]);
  await assert.rejects(preparePublication({ github: global.github, context, output: own, binding: { ...binding, author: bot.login }, sanitize }), /Own-author/);
});
test('default branch advance renders prompt from the admitted policy rather than event revision', async () => {
  const checkout = fs.mkdtempSync(path.join(scratch, 'policy-'));
  const git = (...args) => require('node:child_process').execFileSync('git', ['-C', checkout, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  git('init'); git('config', 'user.name', 'Example Reviewer'); git('config', 'user.email', 'reviewer@example.com');
  const source = path.join(checkout, '.github/workflows/pr-review.md');
  fs.mkdirSync(path.dirname(source), { recursive: true });
  fs.writeFileSync(source, '---\ndescription: old\n---\nOld event policy instructions.');
  git('add', '.github'); git('commit', '-m', 'Old policy');
  const eventPolicy = git('rev-parse', 'HEAD');
  fs.writeFileSync(source, '---\ndescription: new\n---\nNew admitted policy instructions.');
  git('add', '.github'); git('commit', '-m', 'New policy');
  const admittedPolicy = git('rev-parse', 'HEAD');
  const activation = lock.match(/^  activation:\n[\s\S]*?(?=^  [a-z_]+:\n)/m)[0];
  const usesAdmittedPolicy = /name: Checkout .github and .agents folders[\s\S]*?ref: \$\{\{ needs.pre_activation.outputs.policy \}\}/.test(activation);
  git('checkout', '--detach', usesAdmittedPolicy ? admittedPolicy : eventPolicy);
  const prompt = await native('runtime_import.cjs').processRuntimeImports('{{#runtime-import .github/workflows/pr-review.md}}', checkout);
  assert.match(prompt, /New admitted policy instructions/);
  assert.doesNotMatch(prompt, /Old event policy instructions/);
});
test.after(() => fs.rmSync(scratch, { recursive: true, force: true }));
