const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const workflow = fs.readFileSync(path.join(__dirname, '../workflows/claude-review.yml'), 'utf8');
const context = () => ({ repository: 'example/project', event_name: 'pull_request_target', event: {
  repository: { default_branch: 'main' }, sender: { type: 'User' },
  pull_request: { state: 'open', draft: false, head: { repo: { full_name: 'example/project' } }, base: { ref: 'main', repo: { full_name: 'example/project' } } },
} });
test('caller rejects forks, drafts, other bases and disabled dispatch', () => {
  const expression = workflow.match(/    if: >-\n([\s\S]*?)    runs-on:/)?.[1];
  assert.ok(expression);
  const admits = new Function('vars', 'github', `return (${expression});`);
  assert.equal(admits({}, context()), false);
  assert.equal(admits({ CLAUDE_PR_REVIEW_ENABLED: 'true' }, context()), true);
  for (const alter of [c => { c.event.pull_request.head.repo.full_name = 'fork/project'; }, c => { c.event.pull_request.draft = true; }, c => { c.event.pull_request.state = 'closed'; }, c => { c.event.pull_request.base.ref = 'other'; }, c => { c.event_name = 'issue_comment'; }]) {
    const c = context(); alter(c);
    assert.equal(admits({ CLAUDE_PR_REVIEW_ENABLED: 'true' }, c), false);
  }
});
test('worker admits only default-branch dispatch and the known automation invoker', () => {
  const expression = workflow.split('  review:')[1].match(/    if: >-\n([\s\S]*?)    runs-on:/)?.[1];
  assert.ok(expression);
  const admits = new Function('vars', 'github', 'format', `return (${expression});`);
  const c = context(); c.event_name = 'workflow_dispatch'; c.ref = 'refs/heads/main';
  const format = (template, value) => template.replace('{0}', value);
  assert.equal(admits({ CLAUDE_PR_REVIEW_ENABLED: 'true' }, c, format), true);
  c.ref = 'refs/heads/contributor';
  assert.equal(admits({ CLAUDE_PR_REVIEW_ENABLED: 'true' }, c, format), false);
  c.ref = 'refs/heads/main'; c.event.sender = { type: 'Bot', login: 'outside[bot]' };
  assert.equal(admits({ CLAUDE_PR_REVIEW_ENABLED: 'true' }, c, format), false);
  c.event.sender.login = 'github-actions[bot]';
  assert.equal(admits({ CLAUDE_PR_REVIEW_ENABLED: 'true' }, c, format), true);
});
test('official Action stays direct with App OAuth auth and shared configuration', () => {
  const [dispatch, review] = workflow.split('  review:');
  assert.doesNotMatch(dispatch, /secrets\.|id-token:|anthropics\//);
  assert.match(review, /anthropics\/claude-code-action@2dca132ff0e0c4094ce6048b422c6915a071210b/);
  assert.match(review, /claude_code_oauth_token: \$\{\{ secrets.CLAUDE_CODE_OAUTH_TOKEN \}\}/);
  assert.doesNotMatch(review, /anthropic_api_key:|github_token:|contents: write/);
  for (const field of ['launcher', 'prompt', 'claude_args']) assert.ok(review.includes(`steps.prepare.outputs.${field}`));
  assert.match(review, /if: steps.prepare.outputs.eligible == 'true'/);
  assert.match(review, /if: always\(\)\n\s+uses: gumnut-ai\/automation\/actions\/claude-report@/);
  assert.equal((workflow.match(/ref: \$\{\{ github.sha \}\}/g) || []).length, 2);
  assert.doesNotMatch(workflow, /persist-credentials: true|ref: \$\{\{[^}]*(?:head|refs\/pull)/);
  assert.match(fs.readFileSync(path.join(__dirname, 'INSTRUCTIONS.md'), 'utf8'), /Native `Read`, `Grep`, and `Glob`/);
});
test('all shared components use one immutable version', () => {
  const files = ['claude-review.yml', 'pr-approval-request.yml', 'pr-approval-coordinator.yml'];
  const pins = files.flatMap(file => [...fs.readFileSync(path.join(__dirname, '../workflows', file), 'utf8').matchAll(/gumnut-ai\/automation\/actions\/[a-z-]+@([^\s]+)/g)].map(match => match[1]));
  assert.equal(pins.length, 7); assert.equal(new Set(pins).size, 1);
  assert.match(pins[0], /^[a-f0-9]{40}$/);
});
