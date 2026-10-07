const {test}=require('node:test');
const assert=require('node:assert/strict');
const {mkdtempSync,writeFileSync,mkdirSync,rmSync}=require('node:fs');
const {tmpdir}=require('node:os');const {join}=require('node:path');const {execFileSync,spawnSync}=require('node:child_process');
const validator=join(__dirname,'validate-patch.cjs');
const identity={GIT_AUTHOR_NAME:'gumnut-bot[bot]',GIT_AUTHOR_EMAIL:'333470196+gumnut-bot[bot]@users.noreply.github.com',GIT_COMMITTER_NAME:'gumnut-bot[bot]',GIT_COMMITTER_EMAIL:'333470196+gumnut-bot[bot]@users.noreply.github.com'};
function fixture(baseManifest = '[project]\ndependencies = ["example>=1"]\n[tool.uv]\nexclude-newer = "14 days"\n'){const dir=mkdtempSync(join(tmpdir(),'maintainer-test-'));const env={...process.env,...identity};const git=(...args)=>execFileSync('git',args,{cwd:dir,env,encoding:'utf8'});git('init','-q');writeFileSync(join(dir,'pyproject.toml'),baseManifest);mkdirSync(join(dir,'tests'));writeFileSync(join(dir,'tests/test_example.py'),'def test_example(): pass\n');git('add','.');git('commit','-qm','base');env.GITHUB_SHA=git('rev-parse','HEAD').trim();return {dir,env,git,run(){return spawnSync(process.execPath,[validator],{cwd:dir,env,encoding:'utf8'})},cleanup(){rmSync(dir,{recursive:true,force:true})}};}
test('cleanup code is admitted but dependency changes are rejected',()=>{
 for(const file of ['pyproject.toml','package.json','uv.lock','scripts/example.py.lock','app/bun.lock','requirements.txt','Pipfile.lock']) {
  const f=fixture();try{
   const full=join(f.dir,file);mkdirSync(join(full,'..'),{recursive:true});
   writeFileSync(full,'changed dependency content');f.git('add','.');f.git('commit','-qm','dependency edit');
   const result=f.run();assert.notEqual(result.status,0,file);
   assert.match(result.stderr,/dependency manifests and locks belong to Dependabot/,file);
  }finally{f.cleanup()}
 }
 const f=fixture();try{
  writeFileSync(join(f.dir,'service.py'),'def used(): return 1\n');f.git('add','.');f.git('commit','-qm','cleanup');
  assert.equal(f.run().status,0,f.run().stderr);
 }finally{f.cleanup()}
});
test('changed tests and uncommitted candidates fail closed',()=>{const f=fixture();try{writeFileSync(join(f.dir,'tests/test_example.py'),'# skipped\n');assert.match(f.run().stderr,/fully committed/);f.git('add','.');f.git('commit','-qm','test edit');assert.match(f.run().stderr,/sensitive policy/);}finally{f.cleanup()}});
test('workflow and policy files cannot be proposed',()=>{const f=fixture();try{mkdirSync(join(f.dir,'.github'));writeFileSync(join(f.dir,'.github','workflow.yml'),'malicious: true\n');f.git('add','.');f.git('commit','-qm','workflow');assert.match(f.run().stderr,/sensitive policy/);}finally{f.cleanup()}});

test('Biome lint configuration cannot be weakened by cleanup',()=>{
 for(const file of ['app/biome.json','mcp-app/biome.jsonc']) {
  const f=fixture();try{
   const full=join(f.dir,file);mkdirSync(join(full,'..'),{recursive:true});
   writeFileSync(full,'{"linter":{"enabled":false}}');f.git('add','.');f.git('commit','-qm','disable lint');
   assert.match(f.run().stderr,/sensitive policy/,file);
  }finally{f.cleanup()}
 }
});
