const {test}=require('node:test');
const assert=require('node:assert/strict');
const {verifyGraph,assertCoverage,submit}=require('./dependency-coverage.cjs');
function fixture() {
  const urls=Array.from({length:101},(_,n)=>`pkg:npm/package-${n}@1.0.0`);
  const bundle={snapshot:{sha:'a'.repeat(40),ref:'refs/heads/main',manifests:{'app/bun.lock':{resolved:Object.fromEntries(urls.map(u=>[u,{package_url:u}]))}}},inventory:{'app/bun.lock':{count:101,lock_sha256:'lock-hash',excluded:[],limitations:[]}}};
  const state={head:bundle.snapshot.sha,manifests:[{id:'manifest',filename:'app/bun.lock',parseable:true,exceedsMaxSize:false}],urls,jobs:[{name:'index',head_sha:bundle.snapshot.sha,conclusion:'success',steps:[{name:'Submit to GitHub and require complete graph readback',conclusion:'success'}]}],runs:[{id:1,head_sha:bundle.snapshot.sha,conclusion:'success',event:'push',html_url:'index-run'}],posts:0};
  const github={rest:{repos:{getBranch:async()=>({data:{commit:{sha:state.head}}})},actions:{listWorkflowRuns(){},listJobsForWorkflowRun(){}}},paginate:async(method,params)=>{if(method===github.rest.actions.listJobsForWorkflowRun){assert.equal(params.filter,'latest');assert.equal(params.run_id,1);return state.jobs;}return state.runs;},request:async()=>{state.posts++;return {status:201,data:{result:'SUCCESS',id:1}}},graphql:async(query,variables)=>{
    if(query.includes('dependencyGraphManifests'))return {repository:{dependencyGraphManifests:{nodes:state.manifests,pageInfo:{hasNextPage:false,endCursor:null}}}};
    const second=variables.cursor==='next';return {node:{dependencies:{nodes:(second?state.urls.slice(100):state.urls.slice(0,100)).map(packageUrl=>({packageUrl})),pageInfo:{hasNextPage:!second&&state.urls.length>100,endCursor:second?null:'next'}}}};
  }};
  const context={repo:{owner:'example',repo:'example'},sha:bundle.snapshot.sha,payload:{repository:{default_branch:'main'}}};
  return {bundle,state,args:{github,context,bundle},github};
}
test('readback paginates every locked version, not just the first 100',async()=>{
  const f=fixture();assert.equal((await assertCoverage(f.args)).readback['app/bun.lock'].count,101);
});
test('empty graph, dropped version and graph size gaps fail closed',async()=>{
  const f=fixture();f.state.manifests=[];await assert.rejects(()=>verifyGraph({...f.args,repo:f.args.context.repo}),/missing/);
  f.state.manifests=[{id:'manifest',filename:'app/bun.lock',parseable:true,exceedsMaxSize:false}];f.state.urls.pop();
  await assert.rejects(()=>verifyGraph({...f.args,repo:f.args.context.repo}),/differs/);
  f.state.manifests[0].exceedsMaxSize=true;await assert.rejects(()=>verifyGraph({...f.args,repo:f.args.context.repo}),/unparseable/);
});
test('coverage requires successful default-source index and current exact head',async()=>{
  const f=fixture();f.state.runs=[];await assert.rejects(()=>assertCoverage(f.args),/No successful/);
  f.state.head='b'.repeat(40);await assert.rejects(()=>assertCoverage(f.args),/advanced/);
});
test('submission acceptance alone cannot certify coverage',async()=>{
  const f=fixture();f.state.manifests=[];
  await assert.rejects(()=>submit({...f.args,attempts:1,report(){}}),/missing/);assert.equal(f.state.posts,1);
  f.state.head='b'.repeat(40);await assert.rejects(()=>submit({...f.args,attempts:1,report(){}}),/advanced/);assert.equal(f.state.posts,1);
});

test('successful workflow with gated/skipped indexing cannot certify newer-source coverage',async()=>{
  const f=fixture();f.state.jobs[0].conclusion='skipped';f.state.jobs[0].steps=[];
  await assert.rejects(()=>assertCoverage(f.args),/No successful/);
  f.state.jobs[0].conclusion='success';
  f.state.jobs[0].steps=[{name:'Submit to GitHub and require complete graph readback',conclusion:'skipped'}];
  await assert.rejects(()=>assertCoverage(f.args),/No successful/);
  f.state.jobs[0].steps[0].conclusion='success';f.state.jobs[0].head_sha='b'.repeat(40);
  await assert.rejects(()=>assertCoverage(f.args),/No successful/);
});
