// Trusted dispatch-revision validator, mounted read-only outside the candidate.
const {execFileSync}=require('node:child_process');
const fs=require('node:fs');
const {join}=require('node:path');
const git=(...args)=>execFileSync('git',args,{encoding:'utf8'});
const base=process.env.GITHUB_SHA;
if(!/^[0-9a-f]{40}$/.test(base||'')) throw Error('missing immutable dispatch revision');
if(git('status','--porcelain').trim()) throw Error('candidate must be fully committed');
git('diff',base,'HEAD','--check');
const paths=git('diff','--name-only',base,'HEAD').trim().split('\n').filter(Boolean);
for(const path of paths){
 if(path.startsWith('.') || /(^|\/)(tests?|__tests__|database\/alembic\/versions)(\/|$)/.test(path) || /(^|\/)([^/]*config[^/]*|AGENTS\.md|DAEMON\.md|Dockerfile|render\.yaml|[^/]*\.ya?ml)$/.test(path))
   throw Error('sensitive policy/test/config file requires escalation: '+path);
 if(/(^|\/)pyproject\.toml$/.test(path)){
   const old=git('show',base+':'+path), now=fs.readFileSync(path,'utf8');
   execFileSync('uv', ['run', '--no-project', '--no-config', 'python',
     join(__dirname, 'validate-pyproject.py')],
     {input: JSON.stringify([old, now]), encoding:'utf8'});
 }
 if(/(^|\/)package\.json$/.test(path)){
   const configuration=text=>{
     const value=JSON.parse(text);
     for(const field of ['dependencies','devDependencies','peerDependencies','optionalDependencies']) delete value[field];
     return value;
   };
   const {isDeepStrictEqual}=require('node:util');
   if(!isDeepStrictEqual(configuration(git('show',base+':'+path)), configuration(fs.readFileSync(path,'utf8'))))
     throw Error('non-dependency/test/lint package configuration changed: '+path);
 }
 if(/(^|\/)biome\.jsonc?$/.test(path)){
   const strip=s=>s.replace(/https:\/\/biomejs\.dev\/schemas\/[^/]+\/schema\.json/g,'SCHEMA_VERSION');
   if(strip(git('show',base+':'+path))!==strip(fs.readFileSync(path,'utf8'))) throw Error('lint configuration changed: '+path);
 }
}
for(const identity of git('log',base+'..HEAD','--format=%an|%ae|%cn|%ce').trim().split('\n').filter(Boolean)){
 if(identity!=='gumnut-bot[bot]|333470196+gumnut-bot[bot]@users.noreply.github.com|gumnut-bot[bot]|333470196+gumnut-bot[bot]@users.noreply.github.com') throw Error('unexpected proposal commit identity');
}
console.log('candidate policy files/identity/whitespace checked; semantic deny rules still require detection and human review');
