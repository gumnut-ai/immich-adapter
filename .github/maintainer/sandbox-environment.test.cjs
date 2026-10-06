const test=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');const os=require('node:os');
const {spawnSync}=require('node:child_process');const vm=require('node:vm');
const {stripTypeScriptTypes}=require('node:module');
const script=path.join(__dirname,'../../scripts/run_maintainer_sandbox.sh');
const awfSource=process.env.AWF_SOURCE_ROOT;
if(!awfSource) throw Error('AWF_SOURCE_ROOT must identify pinned AWF v0.28.25 source');
function nativeOtel(host,environment={}) {
 const context={process:{env:host},Buffer};vm.createContext(context);
 for(const file of ['src/env-utils.ts','src/services/agent-environment/observability-environment.ts']) {
  const source=fs.readFileSync(path.join(awfSource,file),'utf8').replace(/^import[^;]+;\n/gm,'').replace(/^export /gm,'');
  vm.runInContext(stripTypeScriptTypes(source),context);
 }
 context.buildOtelEnvironment({config:{envAll:false},environment,excludedEnvVars:new Set()});
 return environment;
}
test('sanitized AWF host plus explicit candidate environment withholds inherited secrets',()=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'maintainer-env-'));
 try {
  const stub=path.join(dir,'awf.cjs');
  fs.writeFileSync(stub,'process.stdout.write(JSON.stringify({host:process.env,args:process.argv.slice(2)}))');
  // Replace only the AWF executable; /usr/bin/env -i itself runs unchanged.
  fs.writeFileSync(path.join(dir,'sudo'),`#!${process.execPath}\nconst {spawnSync}=require('node:child_process');const args=process.argv.slice(2);const i=args.indexOf('/usr/local/bin/awf');if(i<0)process.exit(1);args.splice(i,1,${JSON.stringify(process.execPath)},${JSON.stringify(stub)});const r=spawnSync(args[0],args.slice(1),{stdio:'inherit'});process.exit(r.status??1);`);
  fs.chmodSync(path.join(dir,'sudo'),0o755);
  const env={PATH:`${dir}:/usr/bin:/bin`,HOME:dir,USER:'runner',RUNNER_TEMP:dir,GITHUB_WORKSPACE:dir,GITHUB_SHA:'a'.repeat(40),MAINTAINER_SERVICE_PORTS:'5432,6379',RUNNER_TOOL_CACHE:dir,UV_CACHE_DIR:dir};
  for(const key of ['OTEL_EXPORTER_OTLP_HEADERS','OTEL_CUSTOM_SECRET','GH_AW_OTLP_ENDPOINTS','CODEX_API_KEY','OPENAI_API_KEY','GITHUB_TOKEN','GH_TOKEN','GUMBOT_PRIVATE_KEY','MAINTAINER_ALERTS_TOKEN','UNEXPECTED_SECRET']) env[key]='secret-fixture';
  // The pinned builder implicitly copies OTEL_* without --env-all.
  assert.equal(nativeOtel(env).OTEL_CUSTOM_SECRET,'secret-fixture');
  const result=spawnSync('/bin/bash',[script,'candidate'],{env,encoding:'utf8'});
  assert.equal(result.status,0,result.stderr);
  const {host,args}=JSON.parse(result.stdout);assert.ok(!args.includes('--env-all'));
  const candidate={};for(let i=0;i<args.length;i++)if(args[i]==='--env'){const [key,...value]=args[++i].split('=');candidate[key]=value.join('=');}
  nativeOtel(host,candidate);
  assert.deepEqual(Object.keys(candidate).sort(),['CI','GITHUB_SHA','PATH','RUNNER_TEMP','RUNNER_TOOL_CACHE','UV_CACHE_DIR']);
  assert.ok(!Object.values(host).includes('secret-fixture'));
  assert.ok(!Object.values(candidate).includes('secret-fixture'));
  assert.equal(candidate.GITHUB_SHA,env.GITHUB_SHA);
 } finally {fs.rmSync(dir,{recursive:true,force:true});}
});
