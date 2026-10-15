// Run only from the frozen dispatch source, never from the candidate checkout.
const {execFileSync}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
function validateCandidate({source,role,start=source,sourceDirectory=process.cwd(),directory=process.cwd()}) {
  const git=(...args)=>execFileSync('git',args,{cwd:directory,encoding:'utf8'}).trim();
  if(![source,start].every(sha=>/^[a-f0-9]{40}$/.test(sha))) throw Error('invalid candidate revisions');
  if(git('status','--porcelain')) throw Error('candidate must be committed');
  git('merge-base','--is-ancestor',start,'HEAD');
  if(git('rev-list','--merges',`${start}..HEAD`)) throw Error('incremental merge commits forbidden');
  const count=Number(git('rev-list','--count',`${start}..HEAD`));
  if(!count || count>3) throw Error('expected one to three incremental commits');
  const identity='gumnut-bot[bot]|333470196+gumnut-bot[bot]@users.noreply.github.com';
  for(const value of git('log',`${start}..HEAD`,'--format=%an|%ae|%cn|%ce').split('\n'))
    if(value!==`${identity}|${identity}`) throw Error('incremental commits require bot identity');
  const scopeBase=git('merge-base',source,start);
  git('diff',scopeBase,'HEAD','--check');
  const paths=git('diff','--name-only','--no-renames',scopeBase,'HEAD').split('\n').filter(Boolean);
  if(!paths.length) throw Error('empty whole proposal');
  if(git('diff','--raw','--no-renames',scopeBase,'HEAD').split('\n').some(row=>row && !/^:(000000|100644|100755) (000000|100644|100755) /.test(row)))
    throw Error('non-regular candidate files forbidden');
  if(git('diff','--numstat',scopeBase,'HEAD').split('\n').some(row=>row.startsWith('-\t-\t')))
    throw Error('binary candidate files forbidden');
  for(const file of paths) {
    if(git('ls-tree','HEAD','--',file).startsWith('120000')) throw Error('symlink candidate forbidden');
    if(role==='librarian' && !(file==='README.md' || file==='AGENTS.md' || file.endsWith('/AGENTS.md') || file.startsWith('docs/')))
      throw Error('out-of-scope librarian path: '+file);
  }
  if(role==='maintainer') execFileSync(process.execPath,[path.join(sourceDirectory,'.github/maintainer/validate-patch.cjs'),start],{
    cwd:directory,encoding:'utf8',env:{...process.env,GITHUB_SHA:source,FOLLOW_UP_FILE:''},
  });
  return {candidateHead:git('rev-parse','HEAD'),paths};
}
module.exports={validateCandidate};
