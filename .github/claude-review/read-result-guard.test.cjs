const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { allowedRead } = require('./read-result-guard.cjs');

const session = '7e75b7f1-f9a9-4c62-856b-ec92e9f312bb';
function fixture(t) {
  const config = fs.mkdtempSync(path.join(os.tmpdir(), 'review-result-'));
  t.after(() => fs.rmSync(config, { recursive: true, force: true }));
  const results = path.join(config, 'projects', '-example-repo', session, 'tool-results');
  fs.mkdirSync(results, { recursive: true });
  const file = path.join(results, 'call_large.json');
  fs.writeFileSync(file, JSON.stringify([{ type: 'text', text: 'source\n'.repeat(10000) }]));
  return { config, results, file, input: { tool_name: 'Read', session_id: session, tool_input: { file_path: file, offset: 1, limit: 20 } } };
}

test('allows a real oversized result in the current session without rewriting it', t => {
  const f = fixture(t);
  const before = fs.readFileSync(f.file);
  assert.equal(allowedRead(f.input, f.config), true);
  assert.deepEqual(fs.readFileSync(f.file), before);
});

test('rejects checkout files, another session, malformed inputs and escapes', t => {
  const f = fixture(t);
  const outside = path.join(f.config, 'credentials.json');
  fs.writeFileSync(outside, 'fake-secret');
  const other = path.join(f.config, 'projects', '-example-repo', '00000000-0000-0000-0000-000000000000', 'tool-results', 'call.json');
  fs.mkdirSync(path.dirname(other), { recursive: true });
  fs.writeFileSync(other, 'other session');
  for (const file_path of [__filename, outside, other, path.join(f.results, '..', 'private.json'), path.join(f.results, 'missing.json'), 'call_large.json']) {
    assert.equal(allowedRead({ ...f.input, tool_input: { file_path } }, f.config), false, file_path);
  }
  for (const input of [null, {}, { ...f.input, tool_name: 'Write' }, { ...f.input, session_id: '../escape' }, { ...f.input, tool_input: { file_path: 12 } }]) assert.equal(allowedRead(input, f.config), false);
});

test('rejects linked files and linked result directories', t => {
  const f = fixture(t);
  const outside = path.join(f.config, 'private.txt');
  fs.writeFileSync(outside, 'fake-secret');
  const link = path.join(f.results, 'alias.json');
  fs.symlinkSync(outside, link);
  assert.equal(allowedRead({ ...f.input, tool_input: { file_path: link } }, f.config), false);
  fs.rmSync(f.results, { recursive: true });
  const target = path.join(f.config, 'linked');
  fs.mkdirSync(target);
  fs.writeFileSync(path.join(target, 'call_large.json'), 'fake-secret');
  fs.symlinkSync(target, f.results, 'dir');
  assert.equal(allowedRead(f.input, f.config), false);
});

test('hook blocks unsupported paths and malformed stdin without leaking data', t => {
  const f = fixture(t);
  const run = input => spawnSync(process.execPath, [path.join(__dirname, 'read-result-guard.cjs')], { input, encoding: 'utf8', env: { ...process.env, CLAUDE_CONFIG_DIR: f.config } });
  assert.equal(run(JSON.stringify(f.input)).status, 0);
  for (const input of ['{', JSON.stringify({ ...f.input, tool_input: { file_path: __filename } })]) {
    const result = run(input);
    assert.equal(result.status, 2);
    assert.equal(result.stdout, '');
    assert.match(result.stderr, /Read is limited/);
    assert.doesNotMatch(result.stderr, /fake-secret/);
  }
});
