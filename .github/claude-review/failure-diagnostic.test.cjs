const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { failureCategory } = require('./failure-diagnostic.cjs');

test('reports SDK categories even when the result says success with is_error', () => {
  assert.equal(failureCategory([
    { type: 'assistant', error: 'invalid_request' },
    { type: 'result', subtype: 'success', is_error: true },
  ]), 'invalid_request');
  assert.equal(failureCategory([
    { type: 'result', subtype: 'error_max_turns' },
  ]), 'error_max_turns');
});

test('unrecognized or malformed output cannot become log text', () => {
  assert.equal(failureCategory(null), 'diagnostic_unavailable');
  assert.equal(failureCategory([null, { type: 'assistant', error: 'SECRET\n::error::injection' }]), 'unknown');
  assert.equal(failureCategory([{ type: 'result', subtype: 'SECRET' }]), 'unknown');
});

test('CLI withholds transcript secrets and parse errors and handles missing output', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'claude-diagnostic-'));
  const file = path.join(dir, 'claude-execution-output.json');
  const run = () => execFileSync(process.execPath, [path.join(__dirname, 'failure-diagnostic.cjs')], {
    env: { ...process.env, RUNNER_TEMP: dir }, encoding: 'utf8',
  });
  try {
    assert.match(run(), /category: diagnostic_unavailable\./);
    fs.writeFileSync(file, JSON.stringify([
      { type: 'assistant', error: 'authentication_failed', message: { content: [{ type: 'text', text: 'SECRET' }] } },
      { type: 'user', message: { content: [{ type: 'tool_result', content: 'SECRET' }] } },
      { type: 'result', subtype: 'success', is_error: true, result: 'SECRET', errors: ['SECRET'] },
    ]));
    assert.equal(run(), '::notice::Claude review failure category: authentication_failed. Raw execution output withheld.\n');
    fs.writeFileSync(file, 'SECRET malformed JSON');
    assert.equal(run(), '::notice::Claude review failure category: diagnostic_unavailable. Raw execution output withheld.\n');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
