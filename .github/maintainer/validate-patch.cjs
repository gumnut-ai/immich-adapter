// Trusted dispatch-revision validator, mounted read-only outside the candidate.
const {execFileSync}=require('node:child_process');
const git=(...args)=>execFileSync('git',args,{encoding:'utf8'});
const base=process.env.GITHUB_SHA;
if(!/^[0-9a-f]{40}$/.test(base||'')) throw Error('missing immutable dispatch revision');
if(git('status','--porcelain').trim()) throw Error('candidate must be fully committed');
git('diff',base,'HEAD','--check');
const paths=git('diff','--name-only',base,'HEAD').trim().split('\n').filter(Boolean);
for(const path of paths){
 if(path.startsWith('.') || /(^|\/)(tests?|__tests__|database\/alembic\/versions)(\/|$)/.test(path) || /(^|\/)([^/]*config[^/]*|AGENTS\.md|DAEMON\.md|biome\.jsonc?|Dockerfile|render\.yaml|[^/]*\.ya?ml)$/.test(path))
   throw Error('sensitive policy/test/config file requires escalation: '+path);
 if(/(^|\/)(package\.json|pyproject\.toml|[^/]*\.lock|[^/]*\.lockb|package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|requirements[^/]*\.txt|Pipfile|Pipfile\.lock|poetry\.lock)$/.test(path))
   throw Error('dependency manifests and locks belong to Dependabot: '+path);
}
for(const identity of git('log',base+'..HEAD','--format=%an|%ae|%cn|%ce').trim().split('\n').filter(Boolean)){
 if(identity!=='gumnut-bot[bot]|333470196+gumnut-bot[bot]@users.noreply.github.com|gumnut-bot[bot]|333470196+gumnut-bot[bot]@users.noreply.github.com') throw Error('unexpected proposal commit identity');
}
console.log('candidate policy files/identity/whitespace checked; semantic deny rules still require detection and human review');
