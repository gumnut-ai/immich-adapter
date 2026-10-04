const {test}=require('node:test');
const assert=require('node:assert/strict');
const {mkdtempSync,writeFileSync,mkdirSync,rmSync}=require('node:fs');
const {tmpdir}=require('node:os');const {join}=require('node:path');const {execFileSync,spawnSync}=require('node:child_process');
const validator=join(__dirname,'validate-patch.cjs');
const identity={GIT_AUTHOR_NAME:'gumnut-bot[bot]',GIT_AUTHOR_EMAIL:'333470196+gumnut-bot[bot]@users.noreply.github.com',GIT_COMMITTER_NAME:'gumnut-bot[bot]',GIT_COMMITTER_EMAIL:'333470196+gumnut-bot[bot]@users.noreply.github.com'};
function fixture(baseManifest = '[project]\ndependencies = ["example>=1"]\n[tool.uv]\nexclude-newer = "14 days"\n'){const dir=mkdtempSync(join(tmpdir(),'maintainer-test-'));const env={...process.env,...identity};const git=(...args)=>execFileSync('git',args,{cwd:dir,env,encoding:'utf8'});git('init','-q');writeFileSync(join(dir,'pyproject.toml'),baseManifest);mkdirSync(join(dir,'tests'));writeFileSync(join(dir,'tests/test_example.py'),'def test_example(): pass\n');git('add','.');git('commit','-qm','base');env.GITHUB_SHA=git('rev-parse','HEAD').trim();return {dir,env,git,run(){return spawnSync(process.execPath,[validator],{cwd:dir,env,encoding:'utf8'})},cleanup(){rmSync(dir,{recursive:true,force:true})}};}
test('direct bump passes and cooldown changes fail before publication',()=>{const f=fixture();try{writeFileSync(join(f.dir,'pyproject.toml'),'[project]\ndependencies = ["example>=2"]\n[tool.uv]\nexclude-newer = "14 days"\n');f.git('add','.');f.git('commit','-qm','bump');assert.equal(f.run().status,0);writeFileSync(join(f.dir,'pyproject.toml'),'[project]\ndependencies = ["example>=2"]\n[tool.uv]\nexclude-newer = "1 day"\n');f.git('add','.');f.git('commit','-qm','weaken');const r=f.run();assert.notEqual(r.status,0);assert.match(r.stderr,/supply-chain/);}finally{f.cleanup()}});
test('changed tests and uncommitted candidates fail closed',()=>{const f=fixture();try{writeFileSync(join(f.dir,'tests/test_example.py'),'# skipped\n');assert.match(f.run().stderr,/fully committed/);f.git('add','.');f.git('commit','-qm','test edit');assert.match(f.run().stderr,/sensitive policy/);}finally{f.cleanup()}});
test('workflow and policy files cannot be proposed',()=>{const f=fixture();try{mkdirSync(join(f.dir,'.github'));writeFileSync(join(f.dir,'.github','workflow.yml'),'malicious: true\n');f.git('add','.');f.git('commit','-qm','workflow');assert.match(f.run().stderr,/sensitive policy/);}finally{f.cleanup()}});

test('index, source, pytest and type configuration changes fail closed',()=>{
  const base='[project]\ndependencies = ["example>=1"]\n[tool.uv]\nexclude-newer = "14 days"\n[[tool.uv.index]]\nname = "trusted"\nurl = "https://trusted.example/simple"\n[tool.uv.sources]\nexample = { index = "trusted" }\n[tool.pytest.ini_options]\ntestpaths = ["tests", "pure_tests"]\n[tool.ty.rules]\nunused-type-ignore-comment = "warn"\n';
  for(const [before,after] of [['https://trusted.example/simple','https://untrusted.example/simple'],['index = "trusted"','path = "./untrusted"'],['"tests", "pure_tests"','"empty-tests"'],['= "warn"','= "ignore"']]){
    const f=fixture(base);try{writeFileSync(join(f.dir,'pyproject.toml'),base.replace(before,after));f.git('add','.');f.git('commit','-qm','configuration change');const r=f.run();assert.notEqual(r.status,0);assert.match(r.stderr,/non-dependency/);}finally{f.cleanup()}
  }
});
test('declared dependency group version changes remain eligible',()=>{
  const base='[project]\ndependencies = ["example>=1"]\n[dependency-groups]\ndev = ["pytest>=8"]\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n';
  const f=fixture(base);try{writeFileSync(join(f.dir,'pyproject.toml'),base.replace('pytest>=8','pytest>=9'));f.git('add','.');f.git('commit','-qm','test dependency bump');assert.equal(f.run().status,0);}finally{f.cleanup()}
});

test('package version bumps preserve test scripts and committed overrides',()=>{
  const f=fixture();try{
    const manifest={scripts:{test:'vitest run',lint:'biome check .'},dependencies:{example:'^1'}};
    writeFileSync(join(f.dir,'package.json'),JSON.stringify(manifest));f.git('add','.');f.git('commit','-qm','base package');
    f.env.GITHUB_SHA=f.git('rev-parse','HEAD').trim();
    manifest.dependencies.example='^2';writeFileSync(join(f.dir,'package.json'),JSON.stringify(manifest));f.git('add','.');f.git('commit','-qm','version bump');assert.equal(f.run().status,0);
    manifest.scripts.test='echo skipped';writeFileSync(join(f.dir,'package.json'),JSON.stringify(manifest));f.git('add','.');f.git('commit','-qm','test bypass');assert.match(f.run().stderr,/non-dependency/);
    manifest.scripts.test='vitest run';manifest.overrides={transitive:'2'};writeFileSync(join(f.dir,'package.json'),JSON.stringify(manifest));f.git('add','.');f.git('commit','-qm','override');assert.match(f.run().stderr,/non-dependency/);
  }finally{f.cleanup()}
});
