const {test}=require('node:test');
const assert=require('node:assert/strict');
const {verifyGraph,submit}=require('./dependency-coverage.cjs');
function fixture() {
  const urls=Array.from({length:101},(_,n)=>`pkg:npm/package-${n}@1.0.0`);
  const bundle={snapshot:{sha:'a'.repeat(40),ref:'refs/heads/main',manifests:{'app/bun.lock':{resolved:Object.fromEntries(urls.map(u=>[u,{package_url:u}]))}}},inventory:{'app/bun.lock':{count:101,lock_sha256:'lock-hash',excluded:[],limitations:[]}}};
  const state={head:bundle.snapshot.sha,manifests:[{id:'manifest',filename:'app/bun.lock',parseable:true,exceedsMaxSize:false}],urls,posts:0};
  const github={rest:{repos:{getBranch:async()=>({data:{commit:{sha:state.head}}})}},request:async()=>{state.posts++;return {status:201,data:{result:'SUCCESS',id:1}}},graphql:async(query,variables)=>{
    if(query.includes('dependencyGraphManifests'))return {repository:{dependencyGraphManifests:{nodes:state.manifests,pageInfo:{hasNextPage:false,endCursor:null}}}};
    const second=variables.cursor==='next';return {node:{dependencies:{nodes:(second?state.urls.slice(100):state.urls.slice(0,100)).map(packageUrl=>({packageUrl})),pageInfo:{hasNextPage:!second&&state.urls.length>100,endCursor:second?null:'next'}}}};
  }};
  const context={repo:{owner:'example',repo:'example'},sha:bundle.snapshot.sha,payload:{repository:{default_branch:'main'}}};
  return {bundle,state,args:{github,context,bundle},github};
}
test('readback paginates every locked version, not just the first 100',async()=>{
  const f=fixture();assert.equal((await verifyGraph({...f.args,repo:f.args.context.repo}))['app/bun.lock'].count,101);
});
test('empty graph, dropped version and graph size gaps fail closed',async()=>{
  const f=fixture();f.state.manifests=[];await assert.rejects(()=>verifyGraph({...f.args,repo:f.args.context.repo}),/missing/);
  f.state.manifests=[{id:'manifest',filename:'app/bun.lock',parseable:true,exceedsMaxSize:false}];f.state.urls.pop();
  await assert.rejects(()=>verifyGraph({...f.args,repo:f.args.context.repo}),/differs/);
  f.state.manifests[0].exceedsMaxSize=true;await assert.rejects(()=>verifyGraph({...f.args,repo:f.args.context.repo}),/unparseable/);
});
test('submission acceptance alone cannot certify coverage',async()=>{
  const f=fixture();f.state.manifests=[];
  await assert.rejects(()=>submit({...f.args,attempts:1,report(){}}),/missing/);assert.equal(f.state.posts,1);
  f.state.head='b'.repeat(40);await assert.rejects(()=>submit({...f.args,attempts:1,report(){}}),/advanced/);assert.equal(f.state.posts,1);
});
