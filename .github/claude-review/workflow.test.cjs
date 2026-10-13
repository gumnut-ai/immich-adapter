const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const workflow = fs.readFileSync(path.join(__dirname, '../workflows/claude-review.yml'), 'utf8');

// Evaluate the actual admission expression, rather than a second implementation.
const admission = workflow.match(/    if: >-\n([\s\S]*?)    runs-on:/)?.[1];
assert.ok(admission, 'Expected a folded job admission expression');
const eligible = new Function('vars', 'github', `return (${admission});`);
function context() {
  return {
    repository: 'gumnut-ai/immich-adapter',
    event_name: 'pull_request_target',
    event: {
      repository: { default_branch: 'main' },
      sender: { type: 'User' },
      pull_request: {
        state: 'open', draft: false,
        head: { repo: { full_name: 'gumnut-ai/immich-adapter' } },
        base: { ref: 'main', repo: { full_name: 'gumnut-ai/immich-adapter' } },
      },
    },
  };
}

test('disabled and unsafe events cannot reach subscription inference', () => {
  assert.equal(eligible({}, context()), false);
  // These fixtures avoid JS/Actions differences in string case and coercion.
  for (const value of ['false', '', true]) {
    assert.equal(eligible({ CLAUDE_PR_REVIEW_ENABLED: value }, context()), false);
  }
  assert.equal(eligible({ CLAUDE_PR_REVIEW_ENABLED: 'true' }, context()), true);
  for (const alter of [
    c => { c.event.pull_request.draft = true; },
    c => { c.event.pull_request.state = 'closed'; },
    c => { c.event.pull_request.head.repo.full_name = 'attacker/immich-adapter'; },
    c => { c.event.pull_request.base.repo.full_name = 'attacker/immich-adapter'; },
    c => { c.event.pull_request.base.ref = 'contributor-branch'; },
    c => { c.event.sender.type = 'Organization'; },
  ]) {
    const c = context();
    alter(c);
    assert.equal(eligible({ CLAUDE_PR_REVIEW_ENABLED: 'true' }, c), false);
  }
});

test('metadata dispatcher admits Bot senders for subsequent identity validation', () => {
  const c = context();
  c.event.sender = { type: 'Bot', login: 'publisher[bot]' };
  assert.equal(eligible({ CLAUDE_PR_REVIEW_ENABLED: 'true' }, c), true);
});

test('review admission limits bot dispatch to the matching GitHub Actions invoker', () => {
  const expression = workflow.split('  review:')[1].match(/    if: >-\n([\s\S]*?)    runs-on:/)?.[1];
  assert.ok(expression);
  const admits = new Function('vars', 'github', 'format', `return (${expression});`);
  for (const [type, login, actor, expected] of [
    ['User', 'writer', 'writer', true],
    ['Bot', 'github-actions[bot]', 'github-actions[bot]', true],
    ['Bot', 'publisher[bot]', 'publisher[bot]', false],
    // Job admission is preliminary; dispatch.cjs verifies the actor and origin
    // before the step that reads subscription credentials.
    ['Bot', 'github-actions[bot]', 'publisher[bot]', true],
    ['Organization', 'github-actions[bot]', 'github-actions[bot]', false],
  ]) {
    const c = context();
    c.event_name = 'workflow_dispatch';
    c.ref = 'refs/heads/main';
    c.actor = actor;
    c.event.sender = { type, login };
    assert.equal(admits({ CLAUDE_PR_REVIEW_ENABLED: 'true' }, c, (template, value) => template.replace('{0}', value)), expected);
  }
});

test('credentialed job uses trusted policy and no contributor executor', () => {
  assert.match(workflow, /pull_request_target:\n\s+types: \[opened, synchronize, ready_for_review, reopened\]/);
  assert.equal((workflow.match(/ref: \$\{\{ github.sha \}\}/g) || []).length, 2);
  assert.match(workflow, /persist-credentials: false/);
  assert.doesNotMatch(workflow, /ref: \$\{\{[^}]*(?:head|refs\/pull)/);
  assert.doesNotMatch(workflow, /contents: write|allowed_non_write_users:/);
  assert.match(workflow, /allowed_bots: github-actions\n/);
  assert.match(workflow, /id-token: write/);
  assert.doesNotMatch(workflow, /github_token:/);
  assert.match(workflow, /additional_permissions: \|\n\s+contents: read/);
  assert.match(workflow, /claude_code_oauth_token: \$\{\{ secrets.CLAUDE_CODE_OAUTH_TOKEN \}\}/);
  assert.doesNotMatch(workflow, /anthropic_api_key:/);
  assert.match(workflow, /anthropics\/claude-code-action@[a-f0-9]{40} # v\d/);
  assert.match(workflow, /timeout-minutes: \d+/);
  assert.match(workflow, /--max-turns \d+/);
  // Bare mode ignores OAuth credentials and would break subscription authentication.
  assert.doesNotMatch(workflow, /--bare\b/);
  assert.match(workflow, /--restricted\b/);
  assert.match(workflow, /--strict-mcp-config\b/);
  assert.match(workflow, /--permission-mode dontAsk\b/);
  assert.doesNotMatch(workflow, /dangerously-skip-permissions|bypassPermissions|track_progress:/);
  assert.equal(workflow.match(/--tools "([^"]+)"/)?.[1], 'ToolSearch,Agent,Read');
  assert.match(workflow, /Trusted policy SHA: \$\{\{ github.sha \}\}/);
});

test('only GitHub reads, direct comments, and the bounded helper are preapproved', () => {
  const tools = workflow.match(/--allowedTools "([^"]+)"/)?.[1].split(',');
  assert.ok(tools);
  const reads = new Set([
    'ToolSearch', 'Read', 'mcp__github__pull_request_read', 'mcp__github__issue_read',
    'mcp__github__get_file_contents', 'mcp__github__get_commit',
    'mcp__github__list_commits', 'mcp__github__search_code',
  ]);
  const comments = new Set([
    'mcp__github__add_issue_comment', 'mcp__github_inline_comment__create_inline_comment',
  ]);
  for (const tool of tools) assert.ok(reads.has(tool) || comments.has(tool) || tool === 'Agent(haiku-helper)', `Unexpected tool: ${tool}`);
  for (const tool of reads) assert.ok(tools.includes(tool), `Missing read tool: ${tool}`);
  for (const tool of comments) assert.ok(tools.includes(tool), `Missing comment tool: ${tool}`);
  assert.match(workflow, /classify_inline_comments: 'false'/);
  assert.match(workflow, /include_fix_links: 'false'/);
  assert.match(workflow, /steps.claude.outputs.conclusion != 'success'/);
  const agentsPath = workflow.match(/--agents ([^\s]+)/)?.[1];
  assert.ok(agentsPath, 'Expected explicit trusted helper definitions');
  const agents = JSON.parse(fs.readFileSync(path.join(__dirname, '../..', agentsPath), 'utf8'));
  assert.deepEqual(Object.keys(agents), ['haiku-helper']);
  const helper = agents['haiku-helper'];
  assert.equal(helper.model, 'claude-haiku-5-5');
  assert.ok(helper.tools.length > 0);
  for (const tool of helper.tools) assert.ok(reads.has(tool), `Unexpected helper tool: ${tool}`);
  for (const tool of reads) assert.ok(helper.tools.includes(tool), `Missing helper read tool: ${tool}`);
  assert.equal(helper.permissionMode, undefined);
  assert.equal(helper.mcpServers, undefined);
});

// Keep the PR-triggered job free of inference credentials; only trusted dispatches
// can enter the inference job, and its live/provenance validation runs first.
test('PR metadata dispatch and inference have separate trust gates', () => {
  const dispatch = workflow.split('  dispatch:')[1].split('  review:')[0];
  const review = workflow.split('  review:')[1];
  assert.match(dispatch, /github.event_name == 'pull_request_target'/);
  assert.match(dispatch, /actions: write/);
  assert.doesNotMatch(dispatch, /secrets\.|id-token:|anthropics\//);
  assert.match(dispatch, /dispatch.cjs dispatch/);
  assert.match(review, /github.event_name == 'workflow_dispatch'/);
  assert.match(review, /github.ref == format\('refs\/heads\/\{0\}', github.event.repository.default_branch\)/);
  assert.match(review, /actions: read/);
  assert.match(review, /checks: write/);
  assert.ok(review.indexOf('dispatch.cjs prepare') < review.indexOf('secrets.CLAUDE_CODE_OAUTH_TOKEN'));
  assert.equal((review.match(/if: steps.prepare.outputs.eligible == 'true'/g) || []).length, 2);
  assert.match(review, /dispatch.cjs finish/);
  assert.match(review, /CLAUDE_CONCLUSION: \$\{\{ steps.claude.outputs.conclusion \}\}/);
});

test('automation retains the upstream inline comment server with app credentials', () => {
  const config = JSON.parse(workflow.match(/--mcp-config '([^']+)'/)[1]);
  assert.deepEqual(Object.keys(config.mcpServers).sort(), ['github', 'github_inline_comment']);
  const server = config.mcpServers.github_inline_comment;
  assert.equal(server.command, 'bun', 'Use setup-bun PATH; action-local copy is conditional on allowed_non_write_users');
  assert.deepEqual(server.args, ['--no-env-file', '--config=${GITHUB_ACTION_PATH}/bunfig.toml', 'run', '${GITHUB_ACTION_PATH}/src/mcp/github-inline-comment-server.ts']);
  assert.deepEqual(server.env, { REPO_OWNER: '${CLAUDE_REVIEW_OWNER}', REPO_NAME: '${CLAUDE_REVIEW_REPO}', PR_NUMBER: '${CLAUDE_REVIEW_PR_NUMBER}', CLASSIFY_INLINE_COMMENTS: 'false' });
  assert.equal(server.env.GITHUB_TOKEN, undefined, 'Inherit the upstream exchanged app token');
});

test('native paginated reader replaces the bundled server without broad toolsets or credentials', () => {
  const config = JSON.parse(workflow.match(/--mcp-config '([^']+)'/)[1]);
  const server = config.mcpServers.github;
  assert.equal(server.command, 'docker');
  assert.deepEqual(server.env, {
    GITHUB_PERSONAL_ACCESS_TOKEN: '${GITHUB_TOKEN}',
    GITHUB_HOST: '${GITHUB_SERVER_URL}',
  });
  assert.deepEqual(server.args.slice(0, 7), ['run', '-i', '--rm', '-e', 'GITHUB_PERSONAL_ACCESS_TOKEN', '-e', 'GITHUB_HOST']);
  assert.equal(server.args[7], 'ghcr.io/github/github-mcp-server:v1.12.2@sha256:508a0857ec762b1ab1cece29193345b501fab1dd9d1228a7b617062954cecac6');
  assert.deepEqual(server.args.slice(8, 10), ['stdio', '--toolsets=']);
  assert.equal(server.args.length, 11);
  assert.deepEqual(server.args[10].replace(/^--tools=/, '').split(',').sort(), [
    'add_issue_comment', 'get_commit', 'get_file_contents', 'issue_read',
    'list_commits', 'pull_request_read', 'search_code',
  ]);
  assert.doesNotMatch(workflow, /mcp__github__get_pull_request(?:[,"\s]|_)|mcp__github__get_issue_comments/);
  const policy = fs.readFileSync(path.join(__dirname, 'INSTRUCTIONS.md'), 'utf8');
  assert.match(policy, /get_review_comments.*perPage: 5/);
  assert.match(policy, /pageInfo.endCursor.*after.*hasNextPage/);
});

test('always reports numeric usage through upstream output without publishing transcript', () => {
  const usage = workflow.split('      - name: Report sanitized Claude usage')[1].split('      - name: Require successful inference')[0];
  assert.match(usage, /if: always\(\) && steps.prepare.outputs.eligible == 'true'/);
  assert.match(usage, /CLAUDE_EXECUTION_FILE: \$\{\{ steps.claude.outputs.execution_file \}\}/);
  assert.match(usage, /node .github\/claude-review\/usage.cjs/);
  assert.match(usage, /actions\/upload-artifact@[a-f0-9]{40}/);
  assert.match(usage, /path: \$\{\{ runner.temp \}\}\/claude-review-usage.json/);
  assert.match(usage, /retention-days: 30/);
  assert.doesNotMatch(usage, /claude-execution-output.json|continue-on-error/);
  assert.doesNotMatch(workflow, /show_full_output: ['"]?true/);
});

// Read is usable by both agents but constrained by the inherited trusted hook.
test('local reads use the trusted result-only guard without shell or writes', () => {
  assert.match(workflow, /--settings \.github\/claude-review\/settings.json/);
  const settings = JSON.parse(fs.readFileSync(path.join(__dirname, 'settings.json'), 'utf8'));
  assert.deepEqual(settings.permissions, { deny: ['Read(./**)'] });
  assert.deepEqual(settings.hooks.PreToolUse, [{ matcher: 'Read', hooks: [{ type: 'command', command: 'node "$CLAUDE_PROJECT_DIR/.github/claude-review/read-result-guard.cjs"', timeout: 5 }] }]);
  assert.doesNotMatch(workflow, /--add-dir|--tools "[^"]*(?:Bash|Write|Edit|Grep|Glob)/);
  const policy = fs.readFileSync(path.join(__dirname, 'INSTRUCTIONS.md'), 'utf8');
  assert.match(policy, /`Read`.*saved path.*bounded/);
});
