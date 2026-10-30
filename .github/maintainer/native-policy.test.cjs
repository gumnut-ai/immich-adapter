const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
for (const role of ['dependency-maintenance', 'librarian', 'maintainer']) {
  const source = fs.readFileSync(path.join(__dirname, `../workflows/${role}.md`), 'utf8');
  const lock = fs.readFileSync(path.join(__dirname, `../workflows/${role}.lock.yml`), 'utf8');
  test(`${role} uses native publication with isolated credentials and failure gating`, () => {

    const publisher = lock.match(/^  safe_outputs:\n[\s\S]*?(?=^  [a-z_]+:\n|(?![\s\S]))/m)?.[0];
    assert.ok(publisher, 'missing publisher job');
    const hook = publisher.match(/      - name: Authenticate publication checkout with the publisher App\n([\s\S]*?)(?=      - name:)/)?.[1];
    assert.ok(hook, 'missing publisher checkout credential replacement');
    assert.match(hook, /token: \$\{\{ steps.safe-outputs-app-token.outputs.token \}\}/);
    assert.match(hook, /ref: \$\{\{ github.sha \}\}/);
    assert.match(hook, /persist-credentials: true/);
    assert.match(hook, /contains\(needs.agent.outputs.output_types, 'create_pull_request'\)/);
    assert.match(hook, /contains\(needs.agent.outputs.output_types, 'push_to_pull_request_branch'\)/);
    assert.ok(publisher.indexOf('id: safe-outputs-app-token') < publisher.indexOf('name: Authenticate publication checkout'));
    assert.ok(publisher.indexOf('name: Authenticate publication checkout') < publisher.indexOf('id: process_safe_outputs'));
    const encoded = publisher.match(/GH_AW_SAFE_OUTPUTS_HANDLER_CONFIG: ("[^\n]+")/);
    assert.ok(encoded, 'missing native handlers');
    const handlers = JSON.parse(JSON.parse(encoded[1]));
    assert.ok(handlers.create_pull_request, 'creation must remain enabled');
    if (role === 'librarian' || role === 'dependency-maintenance') {
      const repair = handlers.push_to_pull_request_branch;
      assert.ok(repair, 'existing PR repair must remain available');
      assert.equal(repair.target, '*');
      assert.equal(repair.fallback_as_pull_request, false, 'failed repairs must retain the existing PR identity');
      if (role === 'librarian') assert.deepEqual(repair.required_labels, ['librarian']);
      const agent = lock.match(/^  agent:\n[\s\S]*?(?=^  [a-z_]+:\n|(?![\s\S]))/m)?.[0];
      assert.ok(agent, 'missing inference job');
      const permissions = agent.match(/    permissions:\n([\s\S]*?)(?=    [a-z_]+:)/)?.[1];
      assert.ok(permissions, 'missing inference permissions');
      assert.doesNotMatch(permissions, /: write/, 'repair credentials must remain isolated from inference');
      const agentConfig = agent.match(/GH_AW_SAFE_OUTPUTS_CONFIG: ("[^\n]+")/);
      assert.ok(agentConfig, 'missing inference tool configuration');
      assert.deepEqual(JSON.parse(JSON.parse(agentConfig[1])).push_to_pull_request_branch, repair);
    }
    for (const handler of [handlers.create_pull_request, handlers.push_to_pull_request_branch].filter(Boolean)) assert.equal(handler.signed_commits, false, 'Git publication must preserve file modes');
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
