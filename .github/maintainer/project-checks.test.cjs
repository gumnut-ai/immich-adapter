const test=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const path=require('node:path');const {spawnSync}=require('node:child_process');
test('the complete Adapter suite runs docs validation from its frozen script lock',()=>{
 const source=fs.readFileSync(path.join(__dirname,'../../scripts/check_maintainer_environment.sh'),'utf8');
 const body=source.slice(source.indexOf('python_checks()'));
 const result=spawnSync('/bin/bash',['-c',`set -euo pipefail\nmode=smoke\nuv(){ echo "UV:$*"; }\n${body}`],{encoding:'utf8',env:{PATH:'/usr/bin:/bin',GITHUB_SHA:'a'.repeat(40)}});
 assert.equal(result.status,0,result.stderr);
 assert.ok(result.stdout.includes(`UV:run --no-config --locked --script scripts/lint_docs.py --base ${'a'.repeat(40)}`),result.stdout);
});
