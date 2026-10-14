'use strict';
// Dummy upstream for opt-in Docker tests. It has no provider credentials and
// never contacts Anthropic or GitHub; every model turn is scripted below.
const http = require('node:http');
const source = Array.from({ length: 1100 }, (_, index) => `fixture line ${String(index).padStart(4, '0')} ${'x'.repeat(48)}`).join('\n') + '\nEXACT_SOURCE_AFTER_PREVIEW\n';
const state = { routes: [], readPaths: [], requests: [], toolCalls: [], parentCalls: 0, helperCalls: 0, recovered: { parent: false, helper: false }, failures: [] };

function recovered(value, depth = 0) {
  if (depth > 8) return false;
  if (typeof value === 'string') {
    if (value === source || value.endsWith(source)) return true;
    const normalized = value.replace(/^\s*\d+[→\t]/gm, '').trim();
    if (normalized === source.trim()) return true;
    // Native Read can append a system reminder after the JSON file contents.
    for (let end = normalized.length; end > 0; end = normalized.lastIndexOf('}', end - 2) + 1) {
      try { if (recovered(JSON.parse(normalized.slice(0, end)), depth + 1)) return true; } catch {}
    }
    try { return recovered(JSON.parse(normalized.slice(0, normalized.lastIndexOf(']') + 1)), depth + 1); } catch { return false; }
  }
  if (Array.isArray(value)) return value.some(item => recovered(item, depth + 1));
  return value && typeof value === 'object' && Object.values(value).some(item => recovered(item, depth + 1));
}
function savedPath(messages) {
  const text = JSON.stringify(messages);
  // Prefer the gateway payload, then Claude's own persisted tool result. Both
  // are credential-free files visible through the same native Read boundary.
  const gateway = text.match(/\/review-payloads\/[^\s"\\<>]+/);
  if (gateway) return gateway[0];
  const cli = text.match(/Full output saved to: ([^\s"\\<>]+)/);
  if (cli) return cli[1];
  throw new Error('Large response did not expose a persisted result path');
}
function blockFor(query) {
  const actor = query.model.startsWith('claude-opus') ? 'parent' : 'helper';
  const stage = ++state[actor + 'Calls'];
  if (stage === 1) return { type: 'tool_use', id: `${actor}_large`, name: 'mcp__github__get_file_contents', input: { owner: 'fixture', repo: 'repo', path: 'fixture.txt', sha: 'a'.repeat(40) } };
  if (stage === 2) { const file_path=savedPath(query.messages); state.readPaths.push({actor,file_path}); return { type: 'tool_use', id: `${actor}_read`, name: 'Read', input: { file_path, offset: 1, limit: 2000 } }; }
  const reads = query.messages.flatMap(message => Array.isArray(message.content) ? message.content : []).filter(block => block.type === 'tool_result' && block.tool_use_id === `${actor}_read` && !block.is_error);
  state.recovered[actor] ||= reads.some(block => recovered(block.content));
  if (actor === 'parent' && stage === 3) return { type: 'tool_use', id: 'parent_helper', name: 'Agent', input: { subagent_type: 'haiku-helper', description: 'Verify exact large result recovery', prompt: 'Fetch the fixture via GitHub MCP and read its full saved output.' } };
  return { type: 'text', text: `${actor} exact source recovered: ${state.recovered[actor]}` };
}
function model(response, query) {
  const block = blockFor(query);
  const message = { id: `fixture_${state.requests.length}`, type: 'message', role: 'assistant', model: query.model, content: [], stop_reason: null, stop_sequence: null, usage: { input_tokens: 100, output_tokens: 50 } };
  const stop = block.type === 'tool_use' ? 'tool_use' : 'end_turn';
  if (!query.stream) { json(response, { ...message, content: [block], stop_reason: stop }); return; }
  response.writeHead(200, { 'content-type': 'text/event-stream' });
  const events = [
    ['message_start', { type: 'message_start', message }],
    ['content_block_start', { type: 'content_block_start', index: 0, content_block: block.type === 'tool_use' ? { ...block, input: {} } : { type: 'text', text: '' } }],
    ['content_block_delta', { type: 'content_block_delta', index: 0, delta: block.type === 'tool_use' ? { type: 'input_json_delta', partial_json: JSON.stringify(block.input) } : { type: 'text_delta', text: block.text } }],
    ['content_block_stop', { type: 'content_block_stop', index: 0 }],
    ['message_delta', { type: 'message_delta', delta: { stop_reason: stop, stop_sequence: null }, usage: { output_tokens: 50 } }],
    ['message_stop', { type: 'message_stop' }],
  ];
  for (const [event, data] of events) response.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  response.end();
}
function json(response, value, status = 200) { response.writeHead(status, { 'content-type': 'application/json' }); response.end(JSON.stringify(value)); }
const tool = name => ({ name, description: `Dummy ${name} fixture`, inputSchema: { type: 'object', properties: { owner: { type: 'string' }, repo: { type: 'string' }, path: { type: 'string' }, sha: { type: 'string' } } } });
const server = http.createServer(async (request, response) => {
  try {
    if(state.routes.length<200)state.routes.push({method:request.method,path:request.url.slice(0,256)});
    if (request.method === 'GET' && request.url === '/state') { json(response, { ...state, source }); return; }
    if (request.method === 'GET' && request.url.startsWith('/v1/models')) { json(response, { data: [], has_more: false }); return; }
    if (request.method === 'GET' && request.url === '/health') { json(response, { ok: true }); return; }
    if (request.method !== 'POST') { response.writeHead(request.method === 'DELETE' ? 204 : 405); response.end(); return; }
    const chunks = []; let size = 0;
    for await (const chunk of request) { size += chunk.length; if (size > 16 * 1024 * 1024) throw Error('Fixture request too large'); chunks.push(chunk); }
    const query = JSON.parse(Buffer.concat(chunks).toString());
    if (request.url.startsWith('/v1/messages/count_tokens')) { json(response, { input_tokens: 100 }); return; }
    if (request.url.startsWith('/v1/messages')) {
      state.requests.push({ path: request.url, headers: request.headers, body: query });
      model(response, query); return;
    }
    if (request.url.startsWith('/mcp')) {
      if (query.id === undefined) { response.writeHead(202); response.end(); return; }
      let result;
      if (query.method === 'initialize') result = { protocolVersion: '2024-11-05', capabilities: { tools: {} }, serverInfo: { name: 'fixture', version: '1' } };
      else if (query.method === 'tools/list') result = { tools: ['get_file_contents', 'push_files', 'create_inline_comment'].map(tool) };
      else if (query.method === 'tools/call') { state.toolCalls.push(query.params); result = { content: [{ type: 'text', text: source }] }; }
      else result = {};
      json(response, { jsonrpc: '2.0', id: query.id, result }); return;
    }
    json(response, { error: 'Unknown fixture route' }, 404);
  } catch (error) {
    state.failures.push(error.message);
    json(response, { error: 'Fixture failed' }, 500);
  }
});
if (require.main === module) server.listen(Number(process.env.PORT || 8080), '0.0.0.0');
module.exports = { source, recovered };
