const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {execFileSync}=require('node:child_process');
const {validateTransport}=require('./check-transport.cjs');
const nativeDirectory=process.env.GH_AW_SETUP_DIR;
if(!nativeDirectory) throw Error('GH_AW_SETUP_DIR must point to pinned helpers');
const {getBundlePathForBranchInRepo}=require(path.join(nativeDirectory,'generate_git_bundle.cjs'));
const identity={GIT_AUTHOR_NAME:'gumnut-bot[bot]',GIT_AUTHOR_EMAIL:'333470196+gumnut-bot[bot]@users.noreply.github.com',GIT_COMMITTER_NAME:'gumnut-bot[bot]',GIT_COMMITTER_EMAIL:'333470196+gumnut-bot[bot]@users.noreply.github.com'};
function fixture(t) {
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'adapter-follow-up-'));
 const env={...process.env,...identity};
 const git=(...args)=>execFileSync('git',args,{cwd:directory,env,encoding:'utf8'}).trim();
 const write=(file,text)=>{fs.mkdirSync(path.dirname(path.join(directory,file)),{recursive:true});fs.writeFileSync(path.join(directory,file),text);};
 for(const file of ['.github/agent-delivery/candidate-policy.cjs','.github/workflows/librarian.lock.yml']) write(file,fs.readFileSync(path.join(__dirname,'../..',file),'utf8'));
 write('docs/example.md','Initial\n');git('init','-q');git('add','.');git('commit','-qm','base');
 const source=git('rev-parse','HEAD'),branch=`librarian/test-${path.basename(directory).toLowerCase()}`;
 git('checkout','-qb',branch);write('docs/example.md','Human addition\n');git('add','.');
 execFileSync('git',['commit','-qm','human contribution'],{cwd:directory,env:{...env,GIT_AUTHOR_NAME:'Human',GIT_AUTHOR_EMAIL:'human@example.com',GIT_COMMITTER_NAME:'Human',GIT_COMMITTER_EMAIL:'human@example.com'}});
 const start=git('rev-parse','HEAD');
 const manifest={source,repository:'example/adapter',role:'librarian',target:{number:1,head:start,base:source,branch,feedback:[{body:'Fix wording'}]}};
 const file=getBundlePathForBranchInRepo(branch,manifest.repository);
 fs.mkdirSync(path.dirname(file),{recursive:true});
 t.after(()=>{fs.rmSync(directory,{recursive:true,force:true});fs.rmSync(file,{force:true});});
 const run=()=>{git('bundle','create',file,`${source}..HEAD`);return validateTransport({manifest,items:[{type:'push_to_pull_request_branch',pull_request_number:1,branch}],nativeDirectory,sourceDirectory:directory});};
 const commit=()=>{git('add','.');git('commit','-qm','follow-up');};
 return {directory,manifest,git,write,commit,run,branch,start,source};
}
test('native bundle appends bot feedback while preserving original human commits',t=>{
 const f=fixture(t);f.write('docs/example.md','Human addition with corrected wording\n');f.commit();
 const result=f.run();assert.equal(result.candidateHead,f.git('rev-parse','HEAD'));assert.deepEqual(result.paths,['docs/example.md']);
});
test('rewriting the selected head is rejected even when the final files are in scope',t=>{
 const f=fixture(t);f.git('reset','--hard',f.source);f.write('docs/example.md','replacement\n');f.commit();assert.throws(f.run,/ancestor|Command failed/);
});
test('whole role diff rejects forbidden existing changes and preserves native protection',t=>{
 for(const file of ['main.py','docs/package.json','.github/config.yml']) {
  const f=fixture(t);f.write(file,'forbidden\n');f.commit();f.manifest.target.head=f.git('rev-parse','HEAD');
  f.write('docs/example.md','small correction\n');f.commit();assert.throws(f.run,/scope|restrictions/);
 }
});
test('advanced main uses the complete original PR scope without importing newer main files',t=>{
 const f=fixture(t);f.git('checkout','--detach',f.source);f.write('main.py','new main content\n');f.commit();f.manifest.source=f.git('rev-parse','HEAD');
 f.git('checkout',f.branch);f.write('docs/example.md','Human addition corrected\n');f.commit();assert.deepEqual(f.run().paths,['docs/example.md']);
});
test('incremental bot identity and three commit budget are enforced',t=>{
 const f=fixture(t);for(let i=0;i<4;i++){f.write('docs/example.md',`Change ${i}\n`);f.commit();}assert.throws(f.run,/three incremental/);
});
