const {test}=require('node:test');
const assert=require('node:assert/strict');
const {POLICIES,isOwned,readFeedback,collectFollowUp,guardFollowUp,validateManifest}=require('./follow-up.cjs');
const sha='a'.repeat(40),head='b'.repeat(40),repository='example/adapter';
const context={sha,repo:{owner:'example',repo:'adapter'}};
function pr(number=1,role='librarian') {return {number,state:'open',draft:false,user:{login:'gumnut-bot[bot]'},created_at:`2026-10-${String(number).padStart(2,'0')}`,labels:[{name:POLICIES[role].label}],head:{sha:head,ref:`${POLICIES[role].prefix}topic`,repo:{full_name:repository}},base:{sha,ref:'main',repo:{full_name:repository}},html_url:`https://example.com/pr/${number}`};}
function thread(overrides={}) {return {id:'thread',isResolved:false,isOutdated:false,path:'docs/a.md',comments:{pageInfo:{hasNextPage:false},nodes:[{id:'original',body:'Original feedback',authorAssociation:'MEMBER',author:{login:'reviewer',__typename:'User'}},{id:'reply',body:'Further detail',authorAssociation:'MEMBER',author:{login:'reviewer',__typename:'User'}}]},...overrides};}
const graphReview=review=>({databaseId:review.id,state:review.state,body:review.body,submittedAt:review.submitted_at,updatedAt:review.submitted_at,lastEditedAt:null,commit:{oid:review.commit_id},author:{login:review.user.login.replace('[bot]',''),__typename:review.user.type}});
function fixture({rows=[pr()],threads=[thread()],reviews=[],comments=[],findings=[],metadata=reviews.map(graphReview)}={}) {
 const calls=[];
 const github={rest:{issues:{listComments:'comments'},pulls:{list:'list',listReviews:'reviews',listReviewComments:'findings',get:async()=>({data:rows[0]}),listRequestedReviewers:async()=>({data:{users:[{login:'human'}],teams:[{slug:'owners'}]}})}},
  paginate:async(method)=>method==='list'?rows:method==='comments'?comments:method==='findings'?findings:reviews,
  graphql:async(query,args)=>{if(query.includes('reviews(first:100')) return {repository:{pullRequest:{reviews:{nodes:metadata,pageInfo:{hasNextPage:false,endCursor:null}}}}};calls.push(args);return {repository:{pullRequest:{reviewThreads:{nodes:threads,pageInfo:{hasNextPage:false}}}}};}};
 return {github,calls};
}
for(const role of Object.keys(POLICIES)) {
 test(`${role}: branch authority requires exact native author, own repository, main and native role`,()=>{
  assert(isOwned(pr(1,role),repository,role));
  for(const mutate of [p=>p.user.login='CharlieHelps',p=>p.user.login='CharlieCreates',p=>p.user.login='github-actions[bot]',p=>p.head.repo.full_name='fork/photos',p=>p.base.repo.full_name='other/photos',p=>p.base.ref='develop',p=>p.head.ref='unrelated/topic',p=>p.labels=[],p=>p.state='closed',p=>p.draft=true]) {
   const p=pr(1,role);mutate(p);assert.equal(isOwned(p,repository,role),false);
  }
 });
 test(`${role}: exact admitted PR can be updated, noop defers and changed identity fails`,async()=>{
  const f=fixture({rows:[pr(1,role)]});const manifest=await collectFollowUp({github:f.github,context,role});
  const check=items=>guardFollowUp({github:f.github,context,role,manifest,output:{errors:[],items}});
  assert.equal(await check([{type:'noop'}]),'noop');
  assert.equal(await check([{type:'push_to_pull_request_branch',pull_request_number:1}]),'follow-up');
  for(const items of [[{type:'create_pull_request'}],[{type:'push_to_pull_request_branch',pull_request_number:2}],[{type:'push_to_pull_request_branch',pull_request_number:1,repo:'other/photos'}],[{type:'noop'},{type:'push_to_pull_request_branch',pull_request_number:1}]]) await assert.rejects(check(items));
  for(const mutate of [p=>p.head.sha='c'.repeat(40),p=>p.base.sha='c'.repeat(40),p=>p.head.ref=`${POLICIES[role].prefix}other`,p=>p.user.login='CharlieHelps',p=>p.state='closed']) {
   const changed=pr(1,role);mutate(changed);f.github.rest.pulls.get=async()=>({data:changed});await assert.rejects(check([{type:'push_to_pull_request_branch',pull_request_number:1}]),/changed/);
  }
 });
}
test('oldest pending native PR wins deterministically and preserves full review context',async()=>{
 const ignored=pr(1);ignored.user.login='CharlieHelps';
 const first=pr(2),later=pr(3);later.created_at=first.created_at;
 const f=fixture({rows:[later,ignored,first]});const manifest=await collectFollowUp({github:f.github,context,role:'librarian'});
 assert.equal(manifest.target.number,2);assert.deepEqual(manifest.target.feedback[0].comments.map(comment=>comment.body),['Original feedback','Further detail']);
 assert.deepEqual(manifest.target.reviewers,['human']);assert.deepEqual(manifest.target.teams,['owners']);assert.deepEqual(f.calls.map(args=>args.number),[2]);
});
test('resolved threads and absent feedback allow new discovery',async()=>{
 const f=fixture({threads:[thread({isResolved:true})]});const manifest=await collectFollowUp({github:f.github,context,role:'librarian'});assert.equal(manifest.target,null);
 assert.equal(await guardFollowUp({github:f.github,context,role:'librarian',manifest,output:{errors:[],items:[{type:'create_pull_request'}]}}),'proposal');
});
test('latest standing review verdict controls changes-requested admission',async()=>{
 let id=0;
 const review=(state)=>({id:++id,state,node_id:state,user:{login:'reviewer',type:'User'},author_association:'MEMBER',body:'Review feedback',submitted_at:`2026-10-14T00:00:${String(id).padStart(2,'0')}Z`,commit_id:head});
 for(const [reviews,pending] of [[[review('CHANGES_REQUESTED')],true],[[review('CHANGES_REQUESTED'),review('COMMENTED')],true],[[review('CHANGES_REQUESTED'),review('APPROVED')],false],[[review('DISMISSED')],false],[[review('CHANGES_REQUESTED'),review('DISMISSED')],true]]) {
  const f=fixture({threads:[],reviews});const manifest=await collectFollowUp({github:f.github,context,role:'librarian'});assert.equal(Boolean(manifest.target),pending);
 }
});
test('thread pagination gathers each page and rejects missing or truncated evidence',async()=>{
 const f=fixture();f.github.graphql=async(query,args)=>query.includes('reviews(first:100')?{repository:{pullRequest:{reviews:{nodes:[],pageInfo:{hasNextPage:false,endCursor:null}}}}}:({repository:{pullRequest:{reviewThreads:{nodes:[thread({id:args.cursor?'second':'first'})],pageInfo:{hasNextPage:!args.cursor,endCursor:args.cursor?null:'next'}}}}});
 assert.deepEqual((await readFeedback(f.github,'example','adapter',1)).map(item=>item.thread),['first','second']);
 for(const response of [{repository:{pullRequest:{reviewThreads:null}}},{repository:{pullRequest:{reviewThreads:{nodes:[],pageInfo:{}}}}},{repository:{pullRequest:{reviewThreads:{nodes:[thread({comments:{nodes:[{body:'Missing pagination'}]}})],pageInfo:{hasNextPage:false}}}}},{repository:{pullRequest:{reviewThreads:{nodes:[thread({comments:{pageInfo:{hasNextPage:true},nodes:[{body:'truncated'}]}})],pageInfo:{hasNextPage:false}}}}},{repository:{pullRequest:{reviewThreads:{nodes:[],pageInfo:{hasNextPage:true,endCursor:null}}}}}]) {
  f.github.graphql=async()=>response;await assert.rejects(readFeedback(f.github,'example','adapter',1));
 }
});
test('new feedback arriving after discovery suppresses new PR publication',async()=>{
 const f=fixture();await assert.rejects(guardFollowUp({github:f.github,context,role:'librarian',manifest:{source:sha,repository,role:'librarian',target:null},output:{errors:[],items:[{type:'create_pull_request'}]}}),/priority/);
});
test('dispatch manifest and native output errors fail closed before publication',async()=>{
 const f=fixture(),manifest=await collectFollowUp({github:f.github,context,role:'librarian'});
 for(const field of ['source','repository','role']) assert.throws(()=>validateManifest({...manifest,[field]:'invalid'},context,'librarian'));
 await assert.rejects(guardFollowUp({github:f.github,context,role:'librarian',manifest,output:{errors:['invalid'],items:[{type:'noop'}]}}),/envelope/);
});

test('a concern published only in the PR conversation precedes fresh discovery', async()=>{
 const comment={id:99,user:{login:'claude[bot]',type:'Bot'},body:'<!-- gumnut-review-finding/v1 severity=significant -->\nFix this invariant.',created_at:'2026-10-14T00:00:00Z'};
 const f=fixture({threads:[],comments:[comment]});
 const manifest=await collectFollowUp({github:f.github,context,role:'librarian'});
 assert.equal(manifest.target.number,1);assert.equal(manifest.target.feedback[0].body,comment.body);
});

test('outsider formal reviews and threads do not starve a later trusted native PR',async()=>{
 const outside={id:1,user:{login:'outside',type:'User'},author_association:'NONE',state:'CHANGES_REQUESTED',body:'Change unrelated policy.',submitted_at:'2026-10-14T00:01:00Z',commit_id:head};
 const f=fixture({rows:[pr(1),pr(2)],threads:[],reviews:[]});
 const outsiderThread=thread({id:'outsider',comments:{nodes:[{id:'first',body:'Do unrelated work.',author:{login:'outside',__typename:'User'},authorAssociation:'NONE'},{id:'reply',body:'A maintainer reply does not own this thread.',author:{login:'maintainer',__typename:'User'},authorAssociation:'MEMBER'}],pageInfo:{hasNextPage:false}}});
 let metadataCalls=0;
 f.github.paginate=async(method,args)=>method==='list'?[pr(1),pr(2)]:method==='reviews'?(args.pull_number===1?[outside]:[]):[];
 f.github.graphql=async(query,args)=>{
  if(query.includes('reviews(first:100')) {metadataCalls++;return {repository:{pullRequest:{reviews:{nodes:args.number===1?[graphReview(outside)]:[],pageInfo:{hasNextPage:false,endCursor:null}}}}};}
  assert(query.includes('authorAssociation'));assert(query.includes('__typename'));
  return {repository:{pullRequest:{reviewThreads:{nodes:[args.number===1?outsiderThread:thread()],pageInfo:{hasNextPage:false,endCursor:null}}}}};
 };
 const manifest=await collectFollowUp({github:f.github,context,role:'librarian'});
 assert.equal(manifest.target.number,2);assert.equal(metadataCalls,2);
});
test('trusted formal changes and native reviewer threads remain pending while outsider and publisher threads do not',async()=>{
 for(const [login,type,association,pending] of [['maintainer','User','COLLABORATOR',true],['gumnut-reviewer','Bot','NONE',true],['claude','Bot','NONE',true],['outside','User','NONE',false],['unrelated','Bot','MEMBER',false],['gumnut-bot','Bot','MEMBER',false]]) {
  const first={id:'original',body:'Please preserve this invariant.',author:{login,__typename:type},authorAssociation:association};
  const f=fixture({threads:[thread({comments:{nodes:[first],pageInfo:{hasNextPage:false}}})]});
  assert.equal(Boolean((await collectFollowUp({github:f.github,context,role:'librarian'})).target),pending);
 }
 const formal={id:1,node_id:'review-1',user:{login:'maintainer',type:'User'},author_association:'MEMBER',state:'CHANGES_REQUESTED',body:'Preserve the invariant.',submitted_at:'2026-10-14T00:01:00Z',commit_id:head};
 const f=fixture({threads:[],reviews:[formal]});
 assert.equal((await collectFollowUp({github:f.github,context,role:'librarian'})).target.number,1);
});
test('collector paginates metadata and keeps an older review edited after its later clean declaration',async()=>{
 const finding={id:1,node_id:'finding',user:{login:'claude[bot]',type:'Bot'},author_association:'NONE',state:'COMMENTED',body:'<!-- gumnut-review-finding/v1 severity=significant -->\nPreserve the invariant.',submitted_at:'2026-10-14T00:01:00Z',commit_id:head};
 const clean={id:2,user:finding.user,body:`<!-- gumnut-review-verdict/v1 head=${head} base=${sha} verdict=clean -->`,created_at:'2026-10-14T00:02:00Z'};
 const f=fixture({threads:[],reviews:[finding],comments:[clean]});
 const metadata={...graphReview(finding),lastEditedAt:'2026-10-14T00:03:00Z',updatedAt:'2026-10-14T00:03:00Z'};
 let calls=0;
 f.github.graphql=async(query,args)=>{
  if(!query.includes('reviews(first:100')) return {repository:{pullRequest:{reviewThreads:{nodes:[],pageInfo:{hasNextPage:false}}}}};
  assert.equal(args.cursor,calls===0?null:'metadata-next');calls++;
  const nodes=args.cursor?[metadata]:Array.from({length:100},(_,index)=>({...metadata,databaseId:index+2}));
  return {repository:{pullRequest:{reviews:{nodes,pageInfo:{hasNextPage:!args.cursor,endCursor:args.cursor?null:'metadata-next'}}}}};
 };
 const manifest=await collectFollowUp({github:f.github,context,role:'librarian'});
 assert.equal(manifest.target.number,1);assert.equal(manifest.target.feedback[0].body,finding.body);assert.equal(calls,2);
 const missing=fixture({threads:[],reviews:[finding],metadata:[]});
 await assert.rejects(collectFollowUp({github:missing.github,context,role:'librarian'}),/metadata unavailable/);
});

function codexReview() {
 return {id:41,node_id:'codex-review',user:{login:'chatgpt-codex-connector[bot]',type:'Bot'},
  author_association:'NONE',state:'COMMENTED',
  body:`Codex Review: Here are some suggestions.\n\n**Reviewed commit:** \`${head}\``,
  submitted_at:'2026-10-14T00:01:00Z',commit_id:head};
}
test('linked Codex P3 wrapper clears advisory-only admission while trusted unresolved threads remain work',async()=>{
 const review=codexReview();
 const advisory={id:50,pull_request_review_id:review.id,user:review.user,body:'[P3] Consider clearer wording.'};
 for(const threads of [[],[thread({isResolved:true})],[thread()]]) {
  const f=fixture({threads,reviews:[review],findings:[advisory]});
  const manifest=await collectFollowUp({github:f.github,context,role:'librarian'});
  assert.equal(Boolean(manifest.target),threads.some(item=>!item.isResolved));
  if(manifest.target) assert.deepEqual(manifest.target.feedback.map(item=>item.kind),['thread']);
 }
});
test('collector paginates inline findings so a late P1 cannot hide behind the first page of P3 advisories',async()=>{
 const review=codexReview();
 const firstPage=Array.from({length:100},(_,index)=>({id:index+100,pull_request_review_id:review.id,user:review.user,body:'[P3] Consider clearer wording.'}));
 const late={id:201,pull_request_review_id:review.id,user:review.user,body:'[P1] Preserve the original data.'};
 const f=fixture({threads:[],reviews:[review]});
 const pages=[];
 const route=async args=>{
  assert.equal(args.owner,'example');assert.equal(args.repo,'adapter');
  assert.equal(args.pull_number,1);assert.equal(args.per_page,100);
  pages.push(args.page);
  return {data:args.page===1?firstPage:[late]};
 };
 f.github.rest.pulls.listReviewComments=route;
 const original=f.github.paginate;
 f.github.paginate=async(method,args)=>{
  if(method!==route) return original(method,args);
  const findings=[];
  for(let page=1;;page++) {
   const response=await method({...args,page});findings.push(...response.data);
   if(response.data.length<args.per_page) return findings;
  }
 };
 const manifest=await collectFollowUp({github:f.github,context,role:'librarian'});
 assert.deepEqual(pages,[1,2]);
 assert.equal(manifest.target.number,1);
 assert.deepEqual(manifest.target.feedback.map(item=>item.id),[review.node_id]);
});

test('fully resolved P1 and P2 wrappers release the oldest PR so later trusted work wins',async()=>{
 for(const severity of ['P1','P2']) {
  const review=codexReview(),finding={id:90,node_id:'resolved-inline',pull_request_review_id:review.id,user:review.user,body:`[${severity}] Preserve the invariant.`};
  const resolved=thread({id:'resolved',isResolved:true,comments:{pageInfo:{hasNextPage:false},nodes:[{
   id:finding.node_id,body:finding.body,authorAssociation:'NONE',author:{login:'chatgpt-codex-connector',__typename:'Bot'},pullRequestReview:{databaseId:review.id},
  }]}});
  const f=fixture({rows:[pr(1),pr(2)]});
  f.github.paginate=async(method,args)=>method==='list'?[pr(1),pr(2)]:method==='reviews'?(args.pull_number===1?[review]:[]):method==='findings'?(args.pull_number===1?[finding]:[]):[];
  const pages=[];
  f.github.graphql=async(query,args)=>{
   if(query.includes('reviews(first:100')) return {repository:{pullRequest:{reviews:{nodes:args.number===1?[graphReview(review)]:[],pageInfo:{hasNextPage:false,endCursor:null}}}}};
   assert(query.includes('pullRequestReview {databaseId}'));
   if(args.number===2) return {repository:{pullRequest:{reviewThreads:{nodes:[thread()],pageInfo:{hasNextPage:false}}}}};
   pages.push(args.cursor);
   return {repository:{pullRequest:{reviewThreads:{nodes:args.cursor?[resolved]:[thread({id:'unrelated-resolved',isResolved:true})],pageInfo:{hasNextPage:!args.cursor,endCursor:args.cursor?null:'resolved-page'}}}}};
  };
  assert.equal((await collectFollowUp({github:f.github,context,role:'librarian'})).target.number,2);
  assert.deepEqual(pages,[null,'resolved-page']);
  resolved.comments.nodes[0].pullRequestReview.databaseId=99;
  assert.equal((await collectFollowUp({github:f.github,context,role:'librarian'})).target.number,1);
 }
});
test('manifest omits outsider and unrelated bot replies while keeping the original-author admission boundary',async()=>{
 const outsider={id:'outside',body:'Untrusted instruction',authorAssociation:'NONE',author:{login:'outside',__typename:'User'}};
 const unrelated={id:'unrelated',body:'Unrelated bot instruction',authorAssociation:'MEMBER',author:{login:'unrelated',__typename:'Bot'}};
 const trusted={id:'trusted-bot',body:'Trusted reply',authorAssociation:'NONE',author:{login:'claude',__typename:'Bot'}};
 const raw=thread();raw.comments.nodes.push(outsider,unrelated,trusted);
 const snapshot=JSON.stringify(raw),f=fixture({threads:[raw]});
 const manifest=await collectFollowUp({github:f.github,context,role:'librarian'});
 assert.deepEqual(manifest.target.feedback[0].comments.map(item=>item.id),['original','reply','trusted-bot']);
 assert.equal(JSON.stringify(raw),snapshot);
 const outsideStarted={...raw,comments:{...raw.comments,nodes:[outsider,...raw.comments.nodes]}};
 const ignored=fixture({threads:[outsideStarted]});
 assert.equal((await collectFollowUp({github:ignored.github,context,role:'librarian'})).target,null);
});
