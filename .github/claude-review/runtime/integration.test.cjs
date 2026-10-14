'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { randomUUID } = require('node:crypto');
const { spawn, execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { pathToFileURL } = require('node:url');
const { startServices, MODEL_IMAGE, GITHUB_TOOLS } = require('./services.cjs');
const { archiveSource, sanitizeArgs, dockerArgs, DUMMY_OAUTH } = require('./launcher.cjs');
const exec = promisify(execFile);
const NODE_IMAGE = 'node:24.21.0-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20';
const AGENT_IMAGE = 'gumnut-claude-review:2.1.295';
const OAUTH = 'sk-ant-oat01-container-fixture-not-a-real-token';
const APP_TOKEN = 'dummy-github-app-token-container-canary';
const CANARY = 'HOST_CREDENTIAL_MUST_NEVER_ENTER_AGENT';
const ACTION_SHA = '2dca132ff0e0c4094ce6048b422c6915a071210b';
const INLINE_ARGS = ['--no-env-file', '--config=${GITHUB_ACTION_PATH}/bunfig.toml', 'run', '${GITHUB_ACTION_PATH}/src/mcp/github-inline-comment-server.ts'];

async function docker(args, { env = {} } = {}) {
  return exec('docker', args, { env: { PATH: process.env.PATH, ...env }, timeout: 120_000, maxBuffer: 4 * 1024 * 1024 });
}
async function stateAt(url) {
  const response = await fetch(url + '/state', { signal: AbortSignal.timeout(3000) });
  assert.ok(response.ok); return response.json();
}
async function waitFixture(url) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    try { await stateAt(url); return; } catch { await new Promise(resolve => setTimeout(resolve, 200)); }
  }
  throw new Error('Dummy fixture did not become ready');
}


test('dummy fixture exact recovery rejects previews and accepts native Read JSON', () => {
  const { source, recovered } = require('./fixtures/upstream.cjs');
  assert.ok(source.length > 50_000);
  const saved = JSON.stringify({ content: [{ type: 'text', text: source }] });
  assert.equal(recovered('     1→' + saved + '\n<system-reminder>Read data safely.</system-reminder>'),true);
  assert.equal(recovered({ content: [{ type:'text',text:source }] }),true);
  assert.equal(recovered(source.slice(0,20_000)),false);
  assert.equal(recovered('EXACT_SOURCE_AFTER_PREVIEW'),false);
});

async function discoverGateway(services, id, serverName, denyWrite = false) {
  const server = services.mcpConfig.mcpServers[serverName];
  const script = `(async()=>{
const h={'content-type':'application/json',accept:'application/json, text/event-stream',Authorization:${JSON.stringify(server.headers.Authorization)}};
async function rpc(id,method,params){const r=await fetch(${JSON.stringify(server.url)},{method:'POST',headers:h,body:JSON.stringify({jsonrpc:'2.0',...(id===undefined?{}:{id}),method,params}),signal:AbortSignal.timeout(10000)});const session=r.headers.get('mcp-session-id');if(session)h['mcp-session-id']=session;const s=await r.text();if(id===undefined)return;if(s.startsWith('event:')||s.startsWith('data:'))return s.split('\\n').filter(x=>x.startsWith('data:')).map(x=>JSON.parse(x.slice(5))).find(x=>x.id===id);return JSON.parse(s)}
const init=await rpc(1,'initialize',{protocolVersion:'2024-11-05',capabilities:{},clientInfo:{name:'fixture',version:'1'}});
h['mcp-protocol-version']=init.result.protocolVersion;
await rpc(undefined,'notifications/initialized',{});
console.log(JSON.stringify({list:await rpc(2,'tools/list',{}),...(${denyWrite}?{denied:await rpc(3,'tools/call',{name:'push_files',arguments:{owner:'fixture',repo:'repo'}})}:{})}));
})().catch(()=>process.exit(1))`;
  return JSON.parse((await docker(['exec', `claude-review-${id}-model`, 'node', '-e', script])).stdout);
}

// Opt-in must fail when Docker, image construction, transport or actual native
// tools fail. Local runs skip rather than substituting unit mocks for this proof.
test('real Docker isolation, reused proxy/gateway and native parent/helper recovery', {
  skip: process.env.CLAUDE_REVIEW_RUN_CONTAINER_TESTS !== '1', timeout: 600_000,
}, async t => {
  assert.equal(process.platform, 'linux', 'Container proof runs on Linux CI');
  await docker(['info']);
  await docker(['image', 'inspect', AGENT_IMAGE]);
  const id = randomUUID();
  const fixtureName = `claude-fixture-${id}`;
  const probeName = `claude-probe-${id}`;
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'claude-container-proof-'));
  let services;
  const diagnostics = { gateway: {}, sidecars: {}, cli: {} };
  const safe = value => [OAUTH,APP_TOKEN,CANARY].reduce((text,secret)=>text.split(secret).join('[dummy-redacted]'),String(value)).slice(-16000);
  const collect = (child, target) => {
    for (const stream of ['stdout','stderr']) child[stream]?.on('data', chunk => { target[stream]=safe((target[stream]||'')+chunk.toString()); });
    child.once('error',error=>{target.error=safe(error.message);});
    child.once('close',(code,signal)=>{target.exit={code,signal};});
    return child;
  };
  t.after(async () => {
    if (services) await services.close();
    for (const name of [probeName, fixtureName]) await docker(['rm', '--force', name]).catch(() => {});
    await fs.rm(dir, { recursive: true, force: true });
  });
  const repo = path.join(dir, 'repository');
  const privateDir = path.join(dir, 'runner', 'action', 'home');
  await fs.mkdir(repo); await fs.mkdir(privateDir, { recursive: true });
  const credentialFile = path.join(privateDir, 'credentials.env');
  await fs.writeFile(credentialFile, CANARY);
  await exec('git', ['init', '--quiet', repo]);
  await fs.mkdir(path.join(repo, '.github', 'claude-review'), { recursive: true });
  await fs.writeFile(path.join(repo, 'trusted.txt'), 'trusted source snapshot\n');
  await fs.writeFile(path.join(repo, '.github', 'claude-review', 'settings.json'), JSON.stringify({
    permissions: { defaultMode: 'dontAsk', deny: ['Bash', 'Write', 'Edit'] },
  }));
  await exec('git', ['-C', repo, 'add', '.']);
  await exec('git', ['-C', repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--quiet', '-m', 'Trusted fixture source']);
  await fs.appendFile(path.join(repo, '.git', 'config'), '\n# ' + CANARY + '\n');
  await fs.writeFile(path.join(repo, 'untracked-credentials.env'), CANARY);
  await docker(['run', '--detach', '--name', fixtureName, '--network', 'bridge',
    '--publish', '127.0.0.1::80', '--env', 'PORT=80', '--mount', `type=bind,source=${path.join(__dirname, 'fixtures')},target=/fixtures,readonly`,
    NODE_IMAGE, 'node', '/fixtures/upstream.cjs']);
  const port = (await docker(['port', fixtureName, '80/tcp'])).stdout.trim();
  assert.match(port, /^127\.0\.0\.1:\d+$/);
  const adminURL = 'http://' + port;
  await waitFixture(adminURL);
  const fixtureIP = (await docker(['inspect', '--format', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', fixtureName])).stdout.trim();
  assert.match(fixtureIP, /^\d+\.\d+\.\d+\.\d+$/);
  const upstreamURL = `http://${fixtureIP}`;
  const env = { PATH: process.env.PATH, GITHUB_WORKSPACE: repo, GITHUB_ACTION_PATH: privateDir,
    GITHUB_TOKEN: APP_TOKEN, CLAUDE_CODE_OAUTH_TOKEN: OAUTH, GITHUB_SERVER_URL: 'https://github.com',
    CLAUDE_REVIEW_OWNER: 'fixture', CLAUDE_REVIEW_REPO: 'repo', CLAUDE_REVIEW_PR_NUMBER: '42',
    CLAUDE_REVIEW_RUNTIME_IMAGE: AGENT_IMAGE };
  const hostConfig = { mcpServers: { github_inline_comment: { command: 'bun', args: INLINE_ARGS, env: { REPO_OWNER: 'fixture', REPO_NAME: 'repo', PR_NUMBER: '42', GITHUB_TOKEN: APP_TOKEN } } } };
  const fixtureDocker = async (args, options = {}) => {
    if (args[0] === 'rm' && args[1] === '--force' && args[2]?.startsWith(`claude-review-${id}-`)) {
      const name=args[2];
      const info={}; diagnostics.sidecars[name]=info;
      const capture=async(key,command)=>{try{const result=await exec('docker',command,{timeout:4000,maxBuffer:32000,env:{PATH:process.env.PATH}});info[key]=safe(result.stdout+result.stderr);}catch(error){info[key]=safe(error.stderr||error.message);}};
      await capture('state',['inspect','--format','{{json .State}}',name]);
      await capture('logs',['logs','--tail','60',name]);
      if (name.endsWith('-model')) await capture('readiness',['exec',name,'node','-e',`Promise.all(['http://localhost:10000/health','http://mcpg:8080/health'].map(async url=>{try{const r=await fetch(url,{signal:AbortSignal.timeout(1500)});return {url,status:r.status,body:await r.text()}}catch(e){return {url,error:e.message}}})).then(x=>console.log(JSON.stringify(x)))`]);
    }
    // Test-only local logging supplies diagnostics before scoped cleanup. The
    // production service retains its existing log-driver and discarded stderr.
    if (args[0] === 'create' && args.includes('--log-driver')) { args=[...args]; args[args.indexOf('--log-driver')+1]='local'; }
    if (args[0] === 'create' && args.includes(MODEL_IMAGE)) {
      // Only the dummy test overrides the official production target.
      args = [...args]; args.splice(args.length - 1, 0, '--env', 'ANTHROPIC_API_TARGET', '--env', 'AWF_MODEL_FALLBACK');
      options = { ...options, env: { ...options.env, ANTHROPIC_API_TARGET: upstreamURL, AWF_MODEL_FALLBACK: '{"enabled":false}' } };
    }
    return docker(args, options);
  };
  const attachDiagnosticGateway = (name,config,gatewayEnv) => {
    const target={}; diagnostics.gateway[name]=target;
    const child=collect(spawn('docker',['start','--attach','--interactive',name],{env:{PATH:process.env.PATH,...gatewayEnv},stdio:['pipe','pipe','pipe']}),target);
    child.stdin.on('error',()=>{}); child.stdin.end(JSON.stringify(config));
    return child;
  };
  const reportFailure = async label => {
    try { const state=await stateAt(adminURL); diagnostics.fixture={routes:state.routes,readPaths:state.readPaths,parentCalls:state.parentCalls,helperCalls:state.helperCalls,recovered:state.recovered,failures:state.failures,requests:state.requests.map(q=>({model:q.body.model,path:q.path,messages:q.body.messages?.length}))}; } catch(error) { diagnostics.fixtureError=safe(error.message); }
    t.diagnostic(label+' fixture '+safe(JSON.stringify(diagnostics.fixture||diagnostics.fixtureError)));
    for(const [name,value] of Object.entries(diagnostics.gateway))t.diagnostic(label+' gateway '+name+' '+safe(JSON.stringify(value)));
    for(const [name,value] of Object.entries(diagnostics.sidecars))t.diagnostic(label+' sidecar '+name+' '+safe(JSON.stringify(value)));
    t.diagnostic(label+' CLI '+safe(JSON.stringify(diagnostics.cli)));
  };
  const startFixtureServices = async options => {
    services = await startServices(options, {
      id, docker: fixtureDocker,
      attachGateway: (name, config, gatewayEnv) => {
        // Reuse the actual gateway, policies, tool allowlists and payload storage;
        // replace only its external backends with the dummy HTTP fixture.
        config.mcpServers = Object.fromEntries(Object.entries(config.mcpServers).map(([server, value]) => [server, {
          type: 'http', url: upstreamURL + '/mcp/' + server, tools: value.tools,
        }]));
        return attachDiagnosticGateway(name,config,gatewayEnv);
      },
    });
    return services;
  };
  const session = path.join(dir, 'services'); await fs.mkdir(session);
  await t.test('genuine GitHub and Bun inline stdio backends initialize with literal workflow paths', async () => {
    assert.ok(process.env.CLAUDE_REVIEW_TRUSTED_ACTION_DIR, 'CI must provide the pinned action checkout with frozen production dependencies');
    const actionDir = await fs.realpath(process.env.CLAUDE_REVIEW_TRUSTED_ACTION_DIR);
    const revision = (await exec('git', ['-C', actionDir, 'rev-parse', 'HEAD'])).stdout.trim();
    assert.equal(revision, ACTION_SHA);
    await fs.access(path.join(actionDir, 'node_modules', '@modelcontextprotocol', 'sdk'));
    try {
      services = await startServices({ sessionDir: session, env: { ...env, GITHUB_ACTION_PATH: actionDir }, mcpConfig: hostConfig }, { id, docker: fixtureDocker, attachGateway: attachDiagnosticGateway });
      for (const [server, expected] of [['github', GITHUB_TOOLS], ['github_inline_comment', ['create_inline_comment']]]) {
        const discovery = await discoverGateway(services, id, server);
        assert.deepEqual(discovery.list.result.tools.map(tool => tool.name).sort(), [...expected].sort());
      }
      assert.equal((await stateAt(adminURL)).requests.length, 0, 'Backend discovery must not start model inference');
    } catch(error) { await reportFailure('Genuine stdio startup/discovery failed'); throw error; }
    finally { if(services) await services.close(); }
  });
  // The production stdio phase has fully closed; recovery now uses the same
  // gateway/proxy with external HTTP mocks and a deterministic large result.
  await startFixtureServices({ sessionDir: session, env, mcpConfig: hostConfig });

  await t.test('agent mount/env/filesystem/network boundary is effective', async () => {
    const source = path.join(dir, 'clean-source'); await fs.mkdir(source);
    await fs.writeFile(path.join(source, 'trusted.txt'), 'trusted source snapshot\n');
    const probe = `
const fs=require('node:fs'),net=require('node:net');
(async()=>{
 const denied=[];for(const p of ${JSON.stringify([credentialFile, path.join(repo, '.git', 'config'), privateDir, '/workspace/.git/config', '/workspace/untracked-credentials.env', '/var/run/docker.sock'])}){try{fs.readFileSync(p);denied.push(false)}catch{denied.push(true)}}
 const writes=[];for(const p of ['/readonly-proof','/workspace/writable-proof']){try{fs.writeFileSync(p,'x');writes.push(false)}catch{writes.push(true)}}
 fs.writeFileSync('/scratch/writable-proof','scratch works');
 const blocked=async(host,port)=>new Promise(resolve=>{const s=net.connect({host,port});s.setTimeout(2500);s.once('connect',()=>{s.destroy();resolve(false)});s.once('error',()=>resolve(true));s.once('timeout',()=>{s.destroy();resolve(true)})});
 const proc=fs.readFileSync('/proc/1/environ','utf8');
 console.log(JSON.stringify({uid:process.getuid(),denied,writes,scratch:fs.readFileSync('/scratch/writable-proof','utf8'),source:fs.readFileSync('/workspace/trusted.txt','utf8'),env:process.env,proc,externalBlocked:await blocked('1.1.1.1',443),fixtureBlocked:await blocked('${fixtureIP}',80)}));
})().catch(()=>process.exit(1));`;
    const args = dockerArgs({ image: AGENT_IMAGE, network: services.network, sourceDir: source, name: probeName, argv: ['-e', probe], modelURL: services.modelURL });
    args.splice(args.indexOf(AGENT_IMAGE), 0, '--entrypoint', 'node');
    const result = JSON.parse((await docker(args, { env: { HOST_CREDENTIAL_CANARY: CANARY, CLAUDE_CODE_OAUTH_TOKEN: OAUTH, GITHUB_TOKEN: APP_TOKEN } })).stdout);
    assert.equal(result.uid, process.getuid());
    assert.ok(result.uid > 0);
    assert.ok(result.denied.every(Boolean)); assert.ok(result.writes.every(Boolean));
    assert.equal(result.scratch, 'scratch works'); assert.equal(result.source, 'trusted source snapshot\n');
    assert.equal(result.env.CLAUDE_CODE_OAUTH_TOKEN, DUMMY_OAUTH);
    for (const secret of [CANARY, OAUTH, APP_TOKEN]) assert.ok(!JSON.stringify(result).includes(secret));
    assert.equal(result.env.ANTHROPIC_API_KEY, undefined); assert.equal(result.env.GITHUB_TOKEN, undefined);
    assert.ok(result.externalBlocked); assert.ok(result.fixtureBlocked);
    const network = JSON.parse((await docker(['network', 'inspect', services.network])).stdout)[0];
    assert.equal(network.Internal, true);
  });

  await t.test('sidecar management endpoints do not disclose upstream credentials', async () => {
    const script = `(async()=>{const urls=['http://localhost:10000/health','http://localhost:10000/reflect','http://localhost:10001/health','http://localhost:10001/reflect','http://mcpg:8080/health','http://mcpg:8080/reflect'];const results=await Promise.all(urls.map(async url=>{const response=await fetch(url,{signal:AbortSignal.timeout(3000)});return {url,status:response.status,text:await response.text()}}));console.log(JSON.stringify(results))})().catch(()=>process.exit(1))`;
    const results = JSON.parse((await docker(['exec', `claude-review-${id}-model`, 'node', '-e', script])).stdout);
    for (const result of results) {
      for (const secret of [OAUTH,APP_TOKEN,CANARY]) assert.ok(!result.text.includes(secret),result.url+' leaked a credential');
      if (result.url.endsWith('/health')) assert.equal(result.status,200);
    }
  });

  await t.test('actual gateway discovery and raw calls filter forbidden writes', async () => {
    const result = await discoverGateway(services, id, 'github', true);
    assert.ok(result.list.result.tools.some(tool => tool.name === 'get_file_contents'));
    assert.ok(!result.list.result.tools.some(tool => tool.name === 'push_files'));
    assert.ok(result.denied.error || result.denied.result?.isError);
    assert.ok(!(await stateAt(adminURL)).toolCalls.some(call => call.name === 'push_files'));
  });

  await t.test('native Opus and Haiku read the exact large persisted result through the proxy', async () => {
    try {
    const actionDir=await fs.realpath(process.env.CLAUDE_REVIEW_TRUSTED_ACTION_DIR);
    const sdkDir=path.join(actionDir,'node_modules','@anthropic-ai','claude-agent-sdk');
    assert.equal(JSON.parse(await fs.readFile(path.join(sdkDir,'package.json'),'utf8')).version,'0.3.295');
    const { query }=await import(pathToFileURL(path.join(sdkDir,'sdk.mjs')).href);
    const sourceDir=path.join(dir,'sdk-source');
    await archiveSource({repository:repo,destination:sourceDir});
    const cliName=`claude-sdk-${id}`;
    const abortController=new AbortController();
    const deadline=setTimeout(()=>abortController.abort(),90000);
    const events=[];
    let conversation;
    try {
      conversation=query({prompt:'Verify exact large fixture recovery, then ask the Haiku helper to do the same.',options:{
        cwd:repo,pathToClaudeCodeExecutable:'/opt/claude/node_modules/.bin/claude',
        env:{PATH:process.env.PATH,CLAUDE_CODE_ENTRYPOINT:'sdk-ts'},abortController,
        model:'claude-opus-5-5',maxTurns:8,permissionMode:'dontAsk',settingSources:[],
        tools:['Read','Agent'],allowedTools:['Read','Agent(haiku-helper)','mcp__github__get_file_contents'],
        mcpServers:services.mcpConfig.mcpServers,
        agents:{'haiku-helper':{description:'Read fixture source',prompt:'Read only the fixture through MCP and Read.',model:'claude-haiku-5-5',tools:['Read','mcp__github__get_file_contents'],maxTurns:6}},
        extraArgs:{restricted:null,settings:'.github/claude-review/settings.json'},
        spawnClaudeCodeProcess: sdkOptions => {
          const argv=sanitizeArgs({argv:sdkOptions.args,repository:repo,mcpConfig:services.mcpConfig,secrets:[OAUTH,APP_TOKEN]});
          diagnostics.cli.argv=argv;
          const args=dockerArgs({image:AGENT_IMAGE,network:services.network,sourceDir,payloadDir:services.payloadDir,name:cliName,argv,modelURL:services.modelURL});
          return collect(spawn('docker',args,{env:{PATH:process.env.PATH},signal:sdkOptions.signal,stdio:['pipe','pipe','pipe']}),diagnostics.cli);
        },
      }});
      for await (const event of conversation) {
        events.push(event);
        if (event.type === 'result') break;
      }
      assert.ok(events.some(event=>event.type==='result'&&!event.is_error));
    } finally { clearTimeout(deadline); conversation?.close(); await docker(['rm','--force',cliName]).catch(()=>{}); }
    const state = await stateAt(adminURL);
    assert.deepEqual(state.failures,[]);
    assert.ok(state.source.length > 50_000);
    assert.deepEqual(state.recovered,{parent:true,helper:true});
    assert.ok(state.parentCalls >= 4); assert.ok(state.helperCalls >= 3);
    assert.ok(state.requests.some(request => request.body.model === 'claude-opus-5-5'));
    assert.ok(state.requests.some(request => request.body.model === 'claude-haiku-5-5'));
    for (const request of state.requests) {
      assert.equal(request.headers.authorization,'Bearer '+OAUTH);
      assert.equal(request.headers['x-api-key'],undefined);
      assert.ok(request.headers['anthropic-beta'].includes('oauth-2025-04-20'));
      assert.ok(['claude-opus-5-5','claude-haiku-5-5'].includes(request.body.model));
      assert.ok(!JSON.stringify(request.body).includes(CANARY));
      assert.ok(!JSON.stringify(request.body).includes(APP_TOKEN));
      assert.ok(!JSON.stringify(request.body).includes(OAUTH));
    }
    assert.ok(events.some(event => event.type === 'result' && !event.is_error));
    } catch(error) { await reportFailure('Pinned SDK/native CLI recovery failed'); throw error; }
  });
});
