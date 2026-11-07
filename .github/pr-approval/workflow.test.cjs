const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const request = fs.readFileSync(path.join(__dirname, '../workflows/pr-approval-request.yml'), 'utf8');
const coordinator = fs.readFileSync(path.join(__dirname, '../workflows/pr-approval-coordinator.yml'), 'utf8');
test('approval request keeps native admission receipt and no publication credentials', () => {
  assert.match(request, /name: Approval request/);
  assert.match(request, /name: Admitted approval request\n\s+if: steps.request.outputs.eligible == 'true'/);
  assert.match(request, /operation: source/);
  assert.doesNotMatch(request, /secrets\.|publish-token:|create-github-app-token/);
  assert.match(request, /ref: \$\{\{ github.event.repository.default_branch \}\}/);
});
test('approval publication keeps protected environment and scoped App token', () => {
  const [assessment, publication] = coordinator.split('  publish:');
  assert.doesNotMatch(assessment, /secrets\.|publish-token:|create-github-app-token/);
  assert.match(assessment, /operation: route/); assert.match(assessment, /operation: inspect/);
  assert.match(publication, /environment: pr-approval-publisher/);
  assert.match(publication, /if: needs.assess.outputs.eligible == 'true' && needs.assess.outputs.actionable == 'true'/);
  assert.match(publication, /permission-pull-requests: write/);
  assert.match(publication, /operation: publish/);
  assert.match(publication, /publish-token: \$\{\{ steps.app.outputs.token \}\}/);
  assert.doesNotMatch(publication, /permission-contents: write|ref: .*head/);
});

test('eligible requests and publication do not require an activation setting', () => {
  assert.doesNotMatch(request + coordinator, /PR_APPROVAL_MODE|\bmode:/);
  assert.match(coordinator, /PR_APPROVAL_BOT_LOGIN \|\| 'gumnut-reviewer\[bot\]'/);
  const expression = request.match(/    if: >-\n([\s\S]*?)    runs-on:/)?.[1];
  assert.ok(expression);
  const admits = new Function('github', `return Boolean(${expression});`);
  assert.equal(admits({ event_name: 'pull_request_target', event: { action: 'opened' } }), true);
  assert.equal(admits({ event_name: 'issue_comment', event: { issue: { pull_request: {} }, comment: { user: { type: 'User', login: 'writer' } } } }), false);
  const publicationIf = coordinator.split('  publish:')[1].match(/    if: ([^\n]+)/)?.[1];
  const publishes = new Function('needs', `return (${publicationIf});`);
  assert.equal(publishes({ assess: { outputs: { eligible: 'true', actionable: 'true' } } }), true);
  assert.equal(publishes({ assess: { outputs: { eligible: 'true', actionable: 'false' } } }), false);
});
