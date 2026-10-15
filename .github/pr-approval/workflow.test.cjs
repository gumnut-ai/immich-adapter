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
  assert.match(publication, /if: vars.PR_APPROVAL_MODE == 'publish'/);
  assert.match(publication, /permission-pull-requests: write/);
  assert.match(publication, /operation: publish/);
  assert.match(publication, /publish-token: \$\{\{ steps.app.outputs.token \}\}/);
  assert.doesNotMatch(publication, /permission-contents: write|ref: .*head/);
});
