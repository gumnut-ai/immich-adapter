// Trusted PreToolUse hook: native Read may only recover this session's MCP output.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

function allowedRead(input, configDir = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), '.claude')) {
  configDir = path.resolve(configDir);
  if (input?.tool_name !== 'Read' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(input.session_id || '')) return false;
  const file = input.tool_input?.file_path;
  if (typeof file !== 'string' || !path.isAbsolute(file)) return false;
  const relative = path.relative(path.join(configDir, 'projects'), file);
  const parts = relative.split(path.sep);
  if (parts.length !== 4 || !/^[A-Za-z0-9_-]+$/.test(parts[0]) || parts[1] !== input.session_id || parts[2] !== 'tool-results' || !/^[A-Za-z0-9_.-]+$/.test(parts[3]) || ['.', '..'].includes(parts[3])) return false;
  try {
    const root = path.join(configDir, 'projects', ...parts.slice(0, 3));
    // Reject a linked directory or leaf rather than allowing a spelling to
    // redirect the reader to credentials or another session's files.
    for (let dir = root; dir !== path.resolve(configDir); dir = path.dirname(dir)) {
      const stat = fs.lstatSync(dir);
      if (!stat.isDirectory() || stat.isSymbolicLink()) return false;
    }
    const stat = fs.lstatSync(file);
    return stat.isFile() && !stat.isSymbolicLink() && fs.realpathSync(file) === path.join(fs.realpathSync(root), parts[3]);
  } catch {
    return false;
  }
}

if (require.main === module) {
  let raw = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', chunk => { raw += chunk; });
  process.stdin.on('end', () => {
    let allowed = false;
    try { allowed = allowedRead(JSON.parse(raw)); } catch {}
    if (!allowed) {
      process.stderr.write('Read is limited to regular files in this session\'s saved tool-results directory.\n');
      process.exitCode = 2;
    }
  });
}

module.exports = { allowedRead };
