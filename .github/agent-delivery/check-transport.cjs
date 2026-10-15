// Validate the exact native artifact with dispatch-source policy; never run candidate code.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {spawnSync, execFileSync} = require('node:child_process');
const crypto = require('node:crypto');
function validateTransport({manifest,items,nativeDirectory,sourceDirectory=process.cwd()}) {
  const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'daemon-transport-'));
  try {
    const home=path.join(temporary,'home');fs.mkdirSync(home);
    const env={PATH:process.env.PATH,HOME:home,GIT_CONFIG_NOSYSTEM:'1',GIT_CONFIG_GLOBAL:'/dev/null'};
    return JSON.parse(execFileSync(process.execPath,[__filename,'--candidate',JSON.stringify({manifest,items,
      nativeDirectory:path.resolve(nativeDirectory),sourceDirectory:path.resolve(sourceDirectory),temporary})],
      {env,encoding:'utf8',stdio:['ignore','pipe','pipe']}));
  } finally {fs.rmSync(temporary,{recursive:true,force:true});}
}
async function candidate({manifest,items,nativeDirectory,sourceDirectory,temporary}) {
  const terminal=items.filter(x=>['create_pull_request','push_to_pull_request_branch','noop'].includes(x.type));
  if(terminal.length!==1) throw new Error('Exactly one native proposal required');
  if(terminal[0].type==='noop') return {outcome:'noop'};
  const proposal=terminal[0],branch=manifest.target?.branch || proposal.branch;
  if(!/^[a-f0-9]{40}$/.test(manifest.source) || typeof branch!=='string' ||
      (manifest.target && proposal.branch && proposal.branch!==manifest.target.branch)) throw new Error('Invalid captured proposal identity');
  global.core=Object.fromEntries(['info','warning','debug','error'].map(key=>[key,()=>{}]));
  const {resolveTransportPaths}=require(path.join(nativeDirectory,'resolve_transport_paths.cjs'));
  const {applyBundleToBranch}=require(path.join(nativeDirectory,'create_pull_request.cjs'));
  const transport=resolveTransportPaths(proposal,manifest.repository);
  const selected=transport.bundlePath || transport.patchPath;
  if(!selected || !fs.lstatSync(selected).isFile()) throw new Error('Missing regular native proposal transport');
  const sha256=crypto.createHash('sha256').update(fs.readFileSync(selected)).digest('hex');
  const repository=path.join(temporary,'repository');fs.mkdirSync(repository);
  const execute=(command,args,options={})=>{
    if(command!=='git') throw new Error('Only native Git object operations allowed');
    const r=spawnSync('git',['-c','core.hooksPath=/dev/null','-c','protocol.file.allow=always',...args],
      {cwd:repository,encoding:'utf8'});
    if(r.error) throw r.error;
    if(r.status!==0 && !options.ignoreReturnCode) throw new Error(r.stderr || 'Native Git operation failed');
    return {exitCode:r.status,stdout:r.stdout,stderr:r.stderr};
  };
  const git=(...args)=>execute('git',args).stdout.trim();
  const execApi={exec:async(command,args,options)=>execute(command,args,options).exitCode,
    getExecOutput:async(command,args,options)=>execute(command,args,options)};
  git('init','-q');git('config','user.name','gumnut-bot[bot]');
  git('config','user.email','333470196+gumnut-bot[bot]@users.noreply.github.com');
  git('fetch','--no-tags',sourceDirectory,`${manifest.source}:refs/base`);
  const start=manifest.target?.head || manifest.source;
  if(!/^[a-f0-9]{40}$/.test(start)) throw new Error('Missing admitted transport base');
  if(start!==manifest.source) {
    try {git('fetch','--no-tags',sourceDirectory,`${start}:refs/prior`);}
    catch {git('fetch','--no-tags',`https://github.com/${manifest.repository}.git`,`${start}:refs/prior`);}
  }
  git('checkout','-q','-B',branch,start);
  if(transport.bundlePath) await applyBundleToBranch(selected,branch,branch,execApi);
  else {
    const {extractPatchBaseCommit}=require(path.join(nativeDirectory,'commit_sha_helpers.cjs'));
    const recorded=extractPatchBaseCommit(fs.readFileSync(selected,'utf8'));
    if((recorded && recorded!==start) || (proposal.base_commit && proposal.base_commit!==start))
      throw new Error('Patch base differs from admitted head');
    git('am','--3way',selected);
  }
  const {validateCandidate}=require(path.join(sourceDirectory,'.github/agent-delivery/candidate-policy.cjs'));
  const {candidateHead,paths}=validateCandidate({source:manifest.source,role:manifest.role,start,sourceDirectory,directory:repository});
  const lock=fs.readFileSync(path.join(sourceDirectory,`.github/workflows/${manifest.role}.lock.yml`),'utf8');
  const match=lock.match(/GH_AW_SAFE_OUTPUTS_HANDLER_CONFIG: ("[^\n]+")/);
  if(!match) throw Error('Missing frozen native policy');
  const policy=JSON.parse(JSON.parse(match[1])).create_pull_request;
  if(paths.length>policy.max_patch_files) throw Error('Whole proposal exceeds native file count limit');
  const {checkFileProtectionPostApply}=require(path.join(nativeDirectory,'manifest_file_helpers.cjs'));
  if(checkFileProtectionPostApply(paths,policy).action!=='allow') throw Error('Whole proposal violates native file restrictions');
  if(!paths.length || sha256!==crypto.createHash('sha256').update(fs.readFileSync(selected)).digest('hex'))
    throw new Error('Empty or changing native proposal transport');
  return {source:manifest.source,validationBase:manifest.source,selectedHead:start,paths,candidateHead,
    transport:{name:path.basename(selected),sha256,kind:transport.bundlePath?'bundle':'patch'}};
}
if(require.main===module) {
  if(process.argv[2]==='--candidate') candidate(JSON.parse(process.argv[3])).then(x=>console.log(JSON.stringify(x))).catch(e=>{console.error(e.message);process.exitCode=1;});
  else {
    try {console.log(JSON.stringify(validateTransport({manifest:JSON.parse(fs.readFileSync(process.argv[2])),
      items:JSON.parse(fs.readFileSync('/tmp/gh-aw/agent_output.json')).items,nativeDirectory:process.argv[3]})));}
    catch(e){console.error(e.message);process.exitCode=1;}
  }
}
module.exports={validateTransport};
