const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { MAX_BYTES, projectUsage, readUsage, summary } = require('./usage.cjs');

function result() {
  return {
    type: 'result', subtype: 'success', is_error: false, num_turns: 12,
    duration_ms: 12345, total_cost_usd: 1.25,
    permission_denials: [{ tool_input: 'SECRET' }],
    usage: { input_tokens: 999999 },
    modelUsage: {
      'claude-opus-5-5': { inputTokens: 100, outputTokens: 200, cacheReadInputTokens: 300, cacheCreationInputTokens: 400, costUSD: 1.2 },
      'claude-haiku-5-5': { inputTokens: 1, outputTokens: 2, cacheReadInputTokens: 3, cacheCreationInputTokens: 4, costUSD: 0.05 },
    },
    result: 'SECRET', errors: ['SECRET'],
  };
}

function directory(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'claude-usage-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

test('latest cumulative model totals include helpers without double counting result events', () => {
  const report = projectUsage([result(), { type: 'assistant', message: 'SECRET' }, result()]);
  assert.equal(report.availability, 'available');
  assert.equal(report.sdk_status, 'success');
  assert.deepEqual(report.tokens, { inputTokens: 101, outputTokens: 202, cacheReadInputTokens: 303, cacheCreationInputTokens: 404 });
  assert.equal(report.total_cost_usd, 1.25);
  assert.equal(report.permission_denials_count, 1);
  assert.equal(report.models.length, 2);
  assert.doesNotMatch(JSON.stringify(report) + summary(report), /SECRET|999999/);
  assert.match(summary(report), /list-price equivalent/);
  assert.match(summary(report), /does not verify PR comment delivery/);
});

test('SDK error and is_error success subtype retain usage without claiming successful execution', () => {
  for (const change of [{ subtype: 'error_max_turns', is_error: true }, { is_error: true }]) {
    const report = projectUsage([{ ...result(), ...change }]);
    assert.equal(report.sdk_status, 'error');
    assert.equal(report.availability, 'available');
    assert.equal(report.tokens.inputTokens, 101);
  }
});

test('malformed numbers and hostile fields never become log, summary or artifact text', () => {
  const fixture = result();
  fixture.subtype = '::error::SECRET';
  fixture.num_turns = '123 SECRET';
  fixture.duration_ms = Infinity;
  fixture.total_cost_usd = NaN;
  fixture.permission_denials = 'SECRET';
  fixture.modelUsage = { 'SECRET\n| malicious |': { inputTokens: -1, outputTokens: 'SECRET', cacheReadInputTokens: Infinity, cacheCreationInputTokens: 0.5, costUSD: 'SECRET', costBasis: 'SECRET' } };
  const report = projectUsage([fixture]);
  assert.equal(report.availability, 'partial');
  assert.equal(report.sdk_status, 'unavailable');
  assert.deepEqual(report.tokens, { inputTokens: null, outputTokens: null, cacheReadInputTokens: null, cacheCreationInputTokens: null });
  assert.equal(report.models[0].model, 'unknown_model_1');
  assert.doesNotMatch(JSON.stringify(report) + summary(report), /SECRET|malicious|Infinity|NaN/);
  assert.match(summary(report), /pricing basis mixed or unknown/);
});

test('missing result, malformed events and excessive model count are unavailable or partial', () => {
  for (const messages of [null, {}, [], [null, { type: 'assistant' }]]) assert.equal(projectUsage(messages).availability, 'unavailable');
  const fixture = result();
  fixture.modelUsage = Object.fromEntries(Array.from({ length: 33 }, (_, i) => [`model${i}`, {}]));
  assert.equal(projectUsage([fixture]).availability, 'partial');
  fixture.modelUsage = null;
  assert.equal(projectUsage([fixture]).availability, 'partial');
});

test('invalid, missing, oversized, unexpected and symlink input is bounded and withheld', t => {
  const dir = directory(t);
  const file = path.join(dir, 'claude-execution-output.json');
  assert.equal(readUsage('', dir).availability, 'unavailable');
  fs.writeFileSync(file, 'SECRET not JSON');
  assert.equal(readUsage(file, dir).availability, 'unavailable');
  fs.writeFileSync(file, Buffer.alloc(MAX_BYTES + 1));
  assert.equal(readUsage(file, dir).availability, 'unavailable');
  fs.writeFileSync(file, JSON.stringify([result()]));
  assert.equal(readUsage(file, dir).availability, 'available');
  assert.equal(readUsage(path.join(dir, 'other.json'), dir).availability, 'unavailable');
  fs.renameSync(file, path.join(dir, 'other.json'));
  fs.symlinkSync(path.join(dir, 'other.json'), file);
  assert.equal(readUsage(file, dir).availability, 'unavailable');
});

test('CLI emits only projection and summary, and writes explicit unavailable on early failure', t => {
  const dir = directory(t);
  const summaryFile = path.join(dir, 'summary.md');
  const execution = path.join(dir, 'claude-execution-output.json');
  for (const fixture of [null, [result()]]) {
    if (fixture) fs.writeFileSync(execution, JSON.stringify(fixture));
    const child = spawnSync(process.execPath, [path.join(__dirname, 'usage.cjs')], { env: { ...process.env, RUNNER_TEMP: dir, CLAUDE_EXECUTION_FILE: '', GITHUB_STEP_SUMMARY: summaryFile }, encoding: 'utf8' });
    assert.equal(child.status, 0);
    assert.equal(child.stdout, '');
    assert.equal(child.stderr, '');
    const artifact = fs.readFileSync(path.join(dir, 'claude-review-usage.json'), 'utf8');
    assert.doesNotMatch(artifact + fs.readFileSync(summaryFile, 'utf8'), /SECRET/);
    assert.equal(JSON.parse(artifact).availability, fixture ? 'available' : 'unavailable');
  }
});
