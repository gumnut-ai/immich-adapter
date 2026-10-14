'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { PassThrough } = require('node:stream');
const { EventEmitter } = require('node:events');
const { promisify } = require('node:util');
const exec = promisify(require('node:child_process').execFile);
const { DUMMY_OAUTH, sanitizeArgs, agentEnv, dockerArgs, archiveSource, hostMcpConfig, runLauncher } = require('./launcher.cjs');
const SECRET = 'dummy-host-credential-canary';
const repository = '/trusted/photos';
const mcpConfig = { mcpServers: { github: { type: 'http', url: 'http://mcp-gateway:80/mcp/github', headers: { Authorization: 'public-session-id' } } } };
const options = { image: 'gumnut-claude-review:2.1.295', network: 'isolated-review-123', sourceDir: '/tmp/session/source', name: 'review-123', argv: [], modelURL: 'http://model-proxy:10001' };

test('agent environment contains only fixed credential-free values', () => {
  const env = agentEnv(options);
  assert.equal(env.CLAUDE_CODE_OAUTH_TOKEN, DUMMY_OAUTH);
  assert.equal(env.ANTHROPIC_BASE_URL, options.modelURL);
  for (const key of ['ANTHROPIC_API_KEY','GITHUB_TOKEN','GH_TOKEN','ANTHROPIC_CUSTOM_HEADERS','CLAUDE_CODE_API_KEY_HELPER','NODE_OPTIONS','RUNNER_TEMP','GITHUB_ACTION_PATH']) assert.equal(env[key],undefined);
  assert.equal(env.HOME,'/scratch/home');
  for (const modelURL of ['http://attacker:10001','https://model-proxy:10001','http://model-proxy:10001/v1','http://model-proxy:10001?auth='+SECRET]) assert.throws(() => agentEnv({modelURL}),/Unsupported/);
});

test('Docker runtime has one internal network and only clean readonly mounts', () => {
  const args = dockerArgs({...options,payloadDir:'/tmp/session/payloads'});
  assert.ok(args.includes('--read-only'));
  assert.ok(args.includes('--cap-drop=ALL'));
  assert.ok(args.includes('--security-opt=no-new-privileges'));
  assert.equal(args[args.indexOf('--user')+1],require('./identity.cjs').user);
  assert.equal(args.filter(value => value === '--network').length,1);
  const mounts = args.filter((value,index) => args[index-1] === '--mount');
  assert.deepEqual(mounts,['type=bind,source=/tmp/session/source,target=/workspace,readonly','type=bind,source=/tmp/session/payloads,target=/review-payloads,readonly']);
  const {uid,gid}=require('./identity.cjs');
  assert.ok(uid>0);
  assert.ok(args.includes(`/scratch:rw,nosuid,nodev,uid=${uid},gid=${gid},mode=700,size=1g`));
  for (const input of [{sourceDir:'/tmp/x,target=/root'},{name:'x;rm'},{network:'host --privileged'},{payloadDir:'/tmp/x,readonly=false'}]) assert.throws(() => dockerArgs({...options,...input}),/Invalid/);
});

test('SDK args preserve streams and replace all credentialed MCP configs', () => {
  const hostConfig = {mcpServers:{github:{command:'docker',env:{GITHUB_PERSONAL_ACCESS_TOKEN:SECRET}}}};
  const argv = ['--output-format','stream-json','--input-format=stream-json','--verbose','--model=claude-opus-5-5','--mcp-config',JSON.stringify(hostConfig),'--mcp-config='+JSON.stringify(hostConfig),'--settings','/trusted/photos/.github/claude-review/settings.json','--agents','.github/claude-review/agents.json','--setting-sources','user,project','--debug-file','/host/home/secret.log','--strict-mcp-config','--permission-mode=dontAsk'];
  assert.deepEqual(hostMcpConfig(argv),hostConfig);
  const clean = sanitizeArgs({argv,repository,mcpConfig,secrets:[SECRET]});
  assert.ok(!JSON.stringify(clean).includes(SECRET));
  assert.ok(!JSON.stringify(clean).includes('/host/home'));
  assert.equal(clean.filter(value => value === '--mcp-config').length,1);
  assert.equal(clean.filter(value => value === '--strict-mcp-config').length,1);
  assert.equal(clean[clean.indexOf('--settings')+1],'.github/claude-review/settings.json');
  assert.equal(clean[clean.indexOf('--setting-sources')+1],'');
  assert.ok(clean.includes('stream-json'));
  assert.deepEqual(clean.slice(clean.indexOf('--add-dir')+1), ['/scratch','/review-payloads']);
});

test('unsupported flags, path escapes and leaked values fail without echoing secrets', () => {
  for (const argv of [ ['--plugin-dir','/host/action'],['--add-dir','/host/home'],['--settings','../home/.claude/settings.json'],['--append-system-prompt',SECRET],['--managed-settings','{}'],['--settings'] ]) {
    assert.throws(() => sanitizeArgs({argv,repository,mcpConfig,secrets:[SECRET]}),error => !error.message.includes(SECRET));
  }
  assert.throws(() => sanitizeArgs({argv:[],repository,mcpConfig:{mcpServers:{github:{env:{token:SECRET}}}},secrets:[SECRET]}),/Credential/);
  assert.throws(() => hostMcpConfig(['--mcp-config','/host/credential.json']),/Expected inline/);
});

test('source snapshot archives HEAD excluding git metadata, untracked files and local edits', async t => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(),'review-source-'));
  t.after(() => fs.rm(dir,{recursive:true,force:true}));
  const repo = path.join(dir,'repo'); const destination = path.join(dir,'source');
  await fs.mkdir(repo);
  await exec('git',['init','--quiet',repo]);
  await fs.writeFile(path.join(repo,'tracked.txt'),'trusted HEAD\n');
  await exec('git',['-C',repo,'add','tracked.txt']);
  await exec('git',['-C',repo,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--quiet','-m','Fixture source']);
  await fs.writeFile(path.join(repo,'tracked.txt'),'local edit\n');
  await fs.writeFile(path.join(repo,'untracked-key'),SECRET);
  await fs.appendFile(path.join(repo,'.git','config'),'\n# '+SECRET+'\n');
  await archiveSource({repository:repo,destination});
  assert.equal(await fs.readFile(path.join(destination,'tracked.txt'),'utf8'),'trusted HEAD\n');
  assert.deepEqual(await fs.readdir(destination),['tracked.txt']);
});

test('failed source archive does not expose untracked content or Git diagnostics', async t => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'review-source-failure-'));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const repo = path.join(dir, 'repo'); const destination = path.join(dir, 'source');
  await exec('git', ['init', '--quiet', repo]);
  await fs.writeFile(path.join(repo, 'untracked-key'), SECRET);
  await assert.rejects(archiveSource({ repository: repo, destination }), { message: 'Source archive failed' });
  assert.deepEqual(await fs.readdir(destination), []);
});

function fakeChild() {
  const child = new EventEmitter();
  child.stdin = new PassThrough(); child.stdout = new PassThrough(); child.stderr = new PassThrough();
  child.kill = signal => {child.emit('close',null,signal);};
  return child;
}

test('launcher preserves SDK stdin/stdout and cleans only its own container', async () => {
  const input = new PassThrough(); const output = new PassThrough(); let text='';
  output.on('data',chunk => {text+=chunk;});
  let serviceOptions; let dockerOptions; let cleaned=false; let command;
  const result = runLauncher({argv:['--output-format','stream-json'],env:{GITHUB_WORKSPACE:repository,PATH:'/usr/bin',CLAUDE_CODE_OAUTH_TOKEN:SECRET,GITHUB_TOKEN:SECRET,ANTHROPIC_API_KEY:SECRET},input,output,
    archive:async () => {},startServices:async value => {serviceOptions=value;return {...options,mcpConfig,close:async () => {cleaned=true;}};},
    dockerExec:async (binary,args) => {command={binary,args};},
    spawnDocker:(binary,args,spawnOptions) => {
      dockerOptions={binary,args,spawnOptions}; const child=fakeChild();
      child.stdin.on('data',chunk => child.stdout.write(chunk));
      child.stdin.on('end',() => {child.stdout.end();setImmediate(() => child.emit('close',0,null));});
      return child;
    },
  });
  input.end('{"type":"user","message":"fixture"}\n');
  assert.equal(await result,0);
  assert.equal(text,'{"type":"user","message":"fixture"}\n');
  assert.equal(serviceOptions.env.GITHUB_TOKEN,SECRET);
  assert.deepEqual(dockerOptions.spawnOptions.env,{PATH:'/usr/bin'});
  assert.ok(!JSON.stringify(dockerOptions.args).includes(SECRET));
  assert.ok(cleaned);
  assert.deepEqual(command.args.slice(0,2),['rm','--force']);
  assert.equal(command.args[2],dockerOptions.args[dockerOptions.args.indexOf('--name')+1]);
  await assert.rejects(fs.stat(serviceOptions.sessionDir),{code:'ENOENT'});
});

test('launcher closes services when rejected host argv prevents spawning', async () => {
  let cleaned=false; let sessionDir;
  await assert.rejects(runLauncher({argv:['--plugin-dir','/host'],env:{GITHUB_WORKSPACE:repository},
    archive:async () => {},startServices:async value => {sessionDir=value.sessionDir;return {...options,mcpConfig,close:async () => {cleaned=true;}};},
    spawnDocker:() => {throw new Error('Should not spawn');},
  }),/Unsupported/);
  assert.ok(cleaned); await assert.rejects(fs.stat(sessionDir),{code:'ENOENT'});
});

test('image pin, lockfile and executable launcher are reproducible', async () => {
  const dockerfile = await fs.readFile(path.join(__dirname,'Dockerfile'),'utf8');
  assert.ok(dockerfile.includes('node:24.21.0-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20'));
  assert.ok(dockerfile.includes('npm ci --omit=dev'));
  const lock = JSON.parse(await fs.readFile(path.join(__dirname,'package-lock.json'),'utf8'));
  assert.equal(lock.packages['node_modules/@anthropic-ai/claude-code'].version,'2.1.295');
  assert.ok((await fs.stat(path.join(__dirname,'launcher.cjs'))).mode & 0o111);
});

test('cancellation during service startup closes the returned services before any CLI spawn', async () => {
  let cleaned=false; let sessionDir;
  await assert.rejects(runLauncher({argv:[],env:{GITHUB_WORKSPACE:repository},
    archive:async () => {},
    startServices:async value => {
      sessionDir=value.sessionDir;
      process.emit('SIGTERM');
      return {...options,mcpConfig,close:async () => {cleaned=true;}};
    },
    spawnDocker:() => {throw new Error('Should not spawn');},
  }),/cancelled/);
  assert.ok(cleaned); await assert.rejects(fs.stat(sessionDir),{code:'ENOENT'});
});

test('cancellation forwards the signal and removes only the active CLI container', async () => {
  let forwarded; let removed; let closed=0;
  const input=new PassThrough(); const output=new PassThrough();
  const code=await runLauncher({argv:[],env:{GITHUB_WORKSPACE:repository,PATH:'/usr/bin'},input,output,
    archive:async () => {},
    startServices:async () => ({...options,mcpConfig,close:async () => {closed++;}}),
    dockerExec:async (_binary,args) => {removed=args;},
    spawnDocker:() => {
      const child=fakeChild();
      child.kill=signal => {forwarded=signal;child.emit('close',null,signal);};
      setImmediate(() => process.emit('SIGTERM'));
      return child;
    },
  });
  assert.equal(code,130); assert.equal(forwarded,'SIGTERM'); assert.equal(closed,1);
  assert.deepEqual(removed.slice(0,2),['rm','--force']);
  assert.match(removed[2],/^claude-review-[a-f0-9-]+$/);
});
