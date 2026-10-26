const {test}=require('node:test');
const assert=require('node:assert/strict');
const {mkdtempSync,writeFileSync,mkdirSync,rmSync}=require('node:fs');
const {tmpdir}=require('node:os');const {join}=require('node:path');const {execFileSync,spawnSync}=require('node:child_process');
const validator=join(__dirname,'validate-patch.cjs');
const identity={GIT_AUTHOR_NAME:'gumnut-bot[bot]',GIT_AUTHOR_EMAIL:'333470196+gumnut-bot[bot]@users.noreply.github.com',GIT_COMMITTER_NAME:'gumnut-bot[bot]',GIT_COMMITTER_EMAIL:'333470196+gumnut-bot[bot]@users.noreply.github.com'};
function fixture(baseManifest = '[project]\ndependencies = ["example>=1"]\n[tool.uv]\nexclude-newer = "14 days"\n'){const dir=mkdtempSync(join(tmpdir(),'maintainer-test-'));const env={...process.env,...identity};const git=(...args)=>execFileSync('git',args,{cwd:dir,env,encoding:'utf8'});git('init','-q');writeFileSync(join(dir,'pyproject.toml'),baseManifest);mkdirSync(join(dir,'tests'));writeFileSync(join(dir,'tests/test_example.py'),'def test_example():\n    assert 1 + 1 == 2\n');git('add','.');git('commit','-qm','base');env.GITHUB_SHA=git('rev-parse','HEAD').trim();return {dir,env,git,run(){return spawnSync(process.execPath,[validator],{cwd:dir,env,encoding:'utf8'})},cleanup(){rmSync(dir,{recursive:true,force:true})}};}
test('direct bump passes and cooldown changes fail before publication',()=>{const f=fixture();try{writeFileSync(join(f.dir,'pyproject.toml'),'[project]\ndependencies = ["example>=2"]\n[tool.uv]\nexclude-newer = "14 days"\n');f.git('add','.');f.git('commit','-qm','bump');assert.equal(f.run().status,0);writeFileSync(join(f.dir,'pyproject.toml'),'[project]\ndependencies = ["example>=2"]\n[tool.uv]\nexclude-newer = "1 day"\n');f.git('add','.');f.git('commit','-qm','weaken');const r=f.run();assert.notEqual(r.status,0);assert.match(r.stderr,/supply-chain/);}finally{f.cleanup()}});
test('ordinary test refactors are eligible after committing the candidate',()=>{
  for(const path of ['tests/unit/api/sync/test_user_converter_names.py','tests/unit/api/test_admin.py','tests/integration/test_public_config.py','tests/unit/api/test_system_config.py','tests/unit/config/test_uvicorn_ws_config.py','test/example.py','src/__tests__/example.test.js']){
    const f=fixture();try{
      mkdirSync(join(f.dir,path,'..'),{recursive:true});
      const initial=path.endsWith('.js')?'test("sum", () => assert.equal(1 + 1, 2));\n':'def test_sum():\n    assert 1 + 1 == 2\n';
      writeFileSync(join(f.dir,path),initial);f.git('add','.');f.git('commit','-qm','initial test');f.env.GITHUB_SHA=f.git('rev-parse','HEAD').trim();
      writeFileSync(join(f.dir,path),initial.replace('1 + 1', '(1 + 1)'));
      assert.match(f.run().stderr,/fully committed/);
      f.git('add','.');f.git('commit','-qm','test refactor');assert.equal(f.run().status,0,path);
    }finally{f.cleanup()}
  }
});
test('YAML and config-named data fixtures remain eligible',()=>{
  for(const path of ['tests/fixtures/public_config.yaml','tests/fixtures/system_config.yml','tests/fixtures/config/example.yaml','tests/fixtures/.sample/config.yaml']){
    const f=fixture();try{
      mkdirSync(join(f.dir,path,'..'),{recursive:true});
      writeFileSync(join(f.dir,path),'enabled: true\n');f.git('add','.');f.git('commit','-qm','base fixture');
      f.env.GITHUB_SHA=f.git('rev-parse','HEAD').trim();
      writeFileSync(join(f.dir,path),'enabled: false\n');f.git('add','.');f.git('commit','-qm','fixture update');
      assert.equal(f.run().status,0,path);
    }finally{f.cleanup()}
  }
});
test('controls remain protected inside test trees',()=>{
  for(const path of ['tests/pytest.ini','tests/conftest.py','tests/unit/pyrightconfig.json','tests/setup.cfg','tests/tox.ini','tests/uv.toml','tests/bunfig.toml','tests/.github/workflows/ci.yml','tests/AGENTS.md','tests/DAEMON.md','tests/CODEOWNERS','database/alembic/versions/001.py']){
    const f=fixture();try{
      mkdirSync(join(f.dir,path,'..'),{recursive:true});writeFileSync(join(f.dir,path),'# control change\n');
      f.git('add','.');f.git('commit','-qm','control change');assert.match(f.run().stderr,/sensitive policy/,path);
    }finally{f.cleanup()}
  }
});
test('actual repository runtime, runner and supply-chain controls remain protected',()=>{
  for(const path of ['config/settings.py','config/immich_version.py','pytest.ini','pyrightconfig.json','renovate.json','scripts/lint_docs.toml','smoke-tests/checkly.config.ts','smoke-tests/tsconfig.json','smoke-tests/bunfig.toml','Dockerfile','render.yaml','.env.test','.agents/daemons/codebase-maintainer/DAEMON.md']){
    const f=fixture();try{
      mkdirSync(join(f.dir,path,'..'),{recursive:true});writeFileSync(join(f.dir,path),'# control change\n');
      f.git('add','.');f.git('commit','-qm','control change');assert.match(f.run().stderr,/sensitive policy/,path);
    }finally{f.cleanup()}
  }
});
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
