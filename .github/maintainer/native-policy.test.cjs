// Run against the setup helpers from the pinned gh-aw release used to compile.
const test=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');
const setup=process.env.GH_AW_SETUP_DIR;
if(!setup) throw Error('GH_AW_SETUP_DIR must identify the pinned gh-aw setup/js directory');
const {checkFileProtection,checkFileProtectionPostApply}=require(path.join(setup,'manifest_file_helpers.cjs'));
const lock=fs.readFileSync(path.join(__dirname,'../workflows/maintainer.lock.yml'),'utf8');
const match=lock.match(/GH_AW_SAFE_OUTPUTS_HANDLER_CONFIG: ("[^\n]+")/);
assert.ok(match,'compiled handler configuration');
const config=JSON.parse(JSON.parse(match[1])).create_pull_request;
test('native publisher admits dependency manifests while policy/config paths stay blocked',()=>{
 assert.equal(config.protected_files_policy,'blocked');
 for(const file of ['package.json','app/web/smoke-tests/package.json','photos-api/pyproject.toml','uv.lock','app/bun.lock']) {
  const patch=`diff --git a/${file} b/${file}\n`;
  assert.equal(checkFileProtection(patch,config).action,'allow',file);
  assert.equal(checkFileProtectionPostApply([file],config).action,'allow',file);
 }
 for(const file of ['.github/workflows/ci.yml','.agents/DAEMON.md','CODEOWNERS']) {
  assert.equal(checkFileProtection(`diff --git a/${file} b/${file}\n`,config).action,'deny',file);
  assert.equal(checkFileProtectionPostApply([file],config).action,'deny',file);
 }
});
test('compiled trusted admission alone has explicit read scopes',()=>{
 const admission=lock.slice(lock.indexOf('  pre_activation:\n'),lock.indexOf('\n  safe_outputs:\n'));
 assert.match(admission,/\n    permissions:\n      contents: read\n      actions: read\n/);
 assert.match(lock,/\npermissions: \{\}\n/);
});
