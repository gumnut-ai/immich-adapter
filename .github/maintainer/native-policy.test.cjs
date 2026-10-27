const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
for (const role of ['dependency-maintenance', 'librarian', 'maintainer']) {
  const source = fs.readFileSync(path.join(__dirname, `../workflows/${role}.md`), 'utf8');
  const lock = fs.readFileSync(path.join(__dirname, `../workflows/${role}.lock.yml`), 'utf8');
  test(`${role} uses native publication with isolated credentials and failure gating`, () => {
    assert.match(source, /read-only: true/);
    assert.match(source, /environment: gumbot-publisher/);
    assert.match(source, /repositories: \[immich-adapter\]/);
    assert.match(source, /needs\.agent\.result == 'success' && needs\.detection\.outputs\.detection_success == 'true'/);
    assert.match(source, /default_branch_result == 'success'/);
    assert.doesNotMatch(source, /DAEMON_DELIVERY_TRUSTED|DEPENDENCY_MAINTENANCE_TRUSTED|handoff_dependency_pr|temporary_id|ai_credits_context|awf --/);
    assert.doesNotMatch(lock, /uses: gumnut-ai\/automation\/actions\/(?:daemon|dependency)-delivery/);
  });
}
test('direct dependency repair never opens a stacked fallback', () => {
  const source = fs.readFileSync(path.join(__dirname, '../workflows/dependency-maintenance.md'), 'utf8');
  assert.match(source, /fallback-as-pull-request: false/);
});
