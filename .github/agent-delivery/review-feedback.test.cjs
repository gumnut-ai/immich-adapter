const {test} = require('node:test');
const assert = require('node:assert/strict');
const {pendingReviewBodies} = require('./review-feedback.cjs');
const review = (login,state,body,id=1) => ({id,node_id:String(id),user:{login,type:login.endsWith('[bot]')?'Bot':'User'},author_association:'MEMBER',state,body,submitted_at:`2026-10-14T00:00:${String(id).padStart(2,'0')}Z`});
test('body-only native blocking findings are work; clean native summaries are not',()=>{
  const finding=review('gumnut-reviewer[bot]','COMMENTED','🔴 blocking: Preserve the previous commit');
  assert.deepEqual(pendingReviewBodies([finding]),[finding]);
  assert.deepEqual(pendingReviewBodies([review('gumnut-reviewer[bot]','COMMENTED','No findings.')]),[]);
  assert.deepEqual(pendingReviewBodies([review('gumnut-reviewer[bot]','COMMENTED','```\n🔴 blocking: example only\n```')]),[]);
});
test('standing automation concerns reuse coordinator classification and later clean clears them',()=>{
  const finding=review('claude[bot]','COMMENTED','<!-- gumnut-review-finding/v1 severity=significant -->\nFix data loss.');
  assert.deepEqual(pendingReviewBodies([finding]),[finding]);
  const clean=review('claude[bot]','COMMENTED',`<!-- gumnut-review-verdict/v1 head=${'a'.repeat(40)} base=${'b'.repeat(40)} verdict=clean -->`,2);
  assert.deepEqual(pendingReviewBodies([finding,clean]),[]);
});
test('human commented instructions and changes requested persist until a later formal clear',()=>{
  const finding=review('human','COMMENTED','Please fix this invariant.');
  assert.deepEqual(pendingReviewBodies([finding]),[finding]);
  assert.deepEqual(pendingReviewBodies([finding,review('human','APPROVED','',2)]),[]);
  const requested=review('human','CHANGES_REQUESTED','Fix this');
  assert.deepEqual(pendingReviewBodies([requested,review('human','COMMENTED','',2)]),[requested]);
});

test('plain human acknowledgements do not occupy the follow-up queue',()=>{
  for(const body of ['Looks good','Thanks!','LGTM. Thanks!','Looks great to me 👍'])
    assert.deepEqual(pendingReviewBodies([review('human','COMMENTED',body)]),[]);
  const finding=review('human','COMMENTED','Looks good, but please fix the lost update.');
  assert.deepEqual(pendingReviewBodies([finding]),[finding]);
});
test('PR conversation findings are pending until a later source clear',()=>{
  const concern={id:5,user:{login:'claude[bot]',type:'Bot'},body:'<!-- gumnut-review-finding/v1 severity=significant -->\nFix lost updates.',created_at:'2026-10-14T00:00:00Z'};
  const clean={id:6,user:{login:'claude[bot]',type:'Bot'},body:`<!-- gumnut-review-verdict/v1 head=${'a'.repeat(40)} base=${'b'.repeat(40)} verdict=clean -->`,created_at:'2026-10-14T00:01:00Z'};
  assert.equal(pendingReviewBodies([], [concern]).length,1);
  assert.deepEqual(pendingReviewBodies([], [concern,clean]),[]);
  const notice={id:7,user:{login:'chatgpt-codex-connector[bot]',type:'Bot'},body:`<!-- codex-pull-request-review-summary -->\n## Codex Review Summary\nThis comment shows the latest Codex review activity on this pull request.\n| Review | Status | Commit | Review trigger |\n| --- | --- | --- | --- |\n| 📝 **Code Review** | ✅ **Completed** | \`aaaaaaa\` | Manual request |`};
  assert.deepEqual(pendingReviewBodies([], [notice]),[]);
});

test('same-second cross-namespace clean cannot erase a concern, regardless of input order',()=>{
  const finding=review('chatgpt-codex-connector[bot]','COMMENTED','[P1] Preserve the transaction');
  const clean={id:50,user:finding.user,body:"Codex Review: Didn't find any major issues.\n\n**Reviewed commit:** `aaaaaaa`",created_at:finding.submitted_at};
  for (const comments of [[clean], [clean, {...clean,id:49}]]) {
    assert.deepEqual(pendingReviewBodies([finding],comments),[finding]);
  }
  assert.deepEqual(pendingReviewBodies([finding],[{...clean,created_at:'2026-10-14T00:01:00Z'}]),[]);
});
test('all findings since the latest clear are retained across review and comment namespaces',()=>{
  const first=review('chatgpt-codex-connector[bot]','COMMENTED','[P1] Fix transaction loss');
  const second={id:20,user:first.user,body:'[P2] Keep the old review request',created_at:'2026-10-14T00:00:02Z'};
  const clean={id:30,user:first.user,body:"Codex Review: Didn't find any major issues.\n\n**Reviewed commit:** `aaaaaaa`",created_at:'2026-10-14T00:00:03Z'};
  assert.deepEqual(pendingReviewBodies([first],[second]).map(item=>item.id),[first.id,second.id]);
  assert.deepEqual(pendingReviewBodies([first],[second,clean]),[]);
  const later={...second,id:40,created_at:'2026-10-14T00:00:04Z'};
  assert.deepEqual(pendingReviewBodies([first],[later,clean,second]).map(item=>item.id),[later.id]);
});

test('trusted human conversation requests use the same acknowledgement policy as formal reviews',()=>{
  const comment={id:80,user:{login:'maintainer',type:'User'},author_association:'MEMBER',body:'Please drop the second paragraph.',created_at:'2026-10-14T00:02:00Z'};
  for (const association of ['OWNER','MEMBER','COLLABORATOR']) {
    const request={...comment,author_association:association};
    assert.deepEqual(pendingReviewBodies([], [request]),[{...request,review_feedback_kind:'comment'}]);
  }
  assert.deepEqual(pendingReviewBodies([], [{...comment,body:'Thanks! Looks good.'}]),[]);
  assert.deepEqual(pendingReviewBodies([], [{...comment,author_association:'NONE'}]),[]);
  assert.deepEqual(pendingReviewBodies([], [{...comment,user:{login:'linear-code[bot]',type:'Bot'}}]),[]);
  const approval=review('maintainer','APPROVED','',59);
  approval.submitted_at='2026-10-14T00:03:00Z';
  assert.deepEqual(pendingReviewBodies([approval],[comment]),[]);
});

test('human commands are informational while substantive appended feedback remains pending',()=>{
  const comment=body=>({id:90,user:{login:'maintainer',type:'User'},author_association:'MEMBER',body,created_at:'2026-10-14T00:02:00Z'});
  for (const body of ['/review','/review focus on security','@gumnut-reviewer review focus on correctness','@gumnut-reviewer review','@codex review','@codex security review',`@codex review <!-- gumnut-codex-head:${'a'.repeat(40)} -->`])
    assert.deepEqual(pendingReviewBodies([], [comment(body)]),[]);
  for (const body of ['@codex review\nPlease preserve the transaction.','/review focus on security\nPlease preserve the transaction.']) {
    const request=comment(body);
    assert.deepEqual(pendingReviewBodies([], [request]).map(item=>item.id),[90]);
  }
});
test('explicit human approval clears prose findings without dismissing formal changes requests',()=>{
  const request={id:90,user:{login:'maintainer',type:'User'},author_association:'MEMBER',body:'Please fix the paragraph.',created_at:'2026-10-14T00:02:00Z'};
  for (const body of ['Thanks, looks good!','LGTM. Thanks!','Approved.','Resolved, thanks.','Done','Fixed!','Addressed.']) {
    const clear={...request,id:91,body,created_at:'2026-10-14T00:03:00Z'};
    assert.deepEqual(pendingReviewBodies([], [request,clear]),[]);
    assert.deepEqual(pendingReviewBodies([], [request,{...clear,created_at:request.created_at}]).map(item=>item.id),[90]);
    const formal=review('maintainer','CHANGES_REQUESTED','Keep formal changes requested',1);
    assert.deepEqual(pendingReviewBodies([formal],[request,clear]),[formal]);
  }
  assert.deepEqual(pendingReviewBodies([], [request,{...request,id:91,body:'Thanks!',created_at:'2026-10-14T00:03:00Z'}]).map(item=>item.id),[90]);
});

const metadata = (item,overrides={}) => ({databaseId:item.id,state:item.state,body:item.body,
 submittedAt:item.submitted_at,updatedAt:item.submitted_at,lastEditedAt:null,
 commit:{oid:item.commit_id},author:{login:item.user.login.replace('[bot]',''),__typename:item.user.type},...overrides});
const submitted = (login,state,body,id=1) => ({...review(login,state,body,id),user:{login,type:login.endsWith('[bot]')?'Bot':'User'},commit_id:'a'.repeat(40)});
test('joined edited review bodies remain pending after a later clean declaration',()=>{
 const finding=submitted('claude[bot]','COMMENTED','<!-- gumnut-review-finding/v1 severity=significant -->\nPreserve the invariant.');
 const clean={id:20,user:finding.user,body:`<!-- gumnut-review-verdict/v1 head=${'a'.repeat(40)} base=${'b'.repeat(40)} verdict=clean -->`,created_at:'2026-10-14T00:00:02Z'};
 const graph=metadata(finding,{lastEditedAt:'2026-10-14T00:00:03Z',updatedAt:'2026-10-14T00:00:03Z'});
 const result=pendingReviewBodies([finding],[clean],[graph]);
 assert.deepEqual(result.map(item=>item.id),[finding.id]);assert.equal(result[0].decision_at,new Date(graph.lastEditedAt).toISOString());
 const {execFileSync}=require('node:child_process');
 const output=execFileSync(process.execPath,[require.resolve('./review-feedback.cjs')],{input:JSON.stringify({reviews:[finding],comments:[clean],metadata:[graph]}),encoding:'utf8'});
 assert.deepEqual(JSON.parse(output),result);
});
test('metadata coverage and exact snapshots are required for every submitted human or bot review',()=>{
 for(const login of ['human','claude[bot]']) {
  const finding=submitted(login,'COMMENTED','Please preserve the invariant.');
  for(const rows of [[],[metadata(finding,{lastEditedAt:'invalid'})],[metadata(finding,{body:'different'})],[metadata(finding,{author:{login:'other',__typename:finding.user.type}})],[metadata(finding),metadata(finding)]])
   assert.throws(()=>pendingReviewBodies([finding],[],rows),/metadata|timestamps/);
 }
});
test('editing formal human approval cannot clear a later finding using its edit time',()=>{
 const approval=submitted('human','APPROVED','Looks good',1);
 const finding={id:2,user:{login:'human',type:'User'},author_association:'MEMBER',body:'Please preserve the invariant.',created_at:'2026-10-14T00:00:02Z'};
 const graph=metadata(approval,{lastEditedAt:'2026-10-14T00:00:03Z',updatedAt:'2026-10-14T00:00:03Z'});
 assert.deepEqual(pendingReviewBodies([approval],[finding],[graph]).map(item=>item.id),[2]);
});
test('async metadata reader reuses coordinator pagination and fails closed on missing or repeated pages',async()=>{
 const {readReviewMetadata,REVIEW_METADATA_QUERY}=require('./review-feedback.cjs');
 const rows=[metadata(submitted('human','COMMENTED','Fix the invariant',1)),metadata(submitted('human','APPROVED','Looks good',2))];
 let calls=0;
 const github={graphql:async(query,variables)=>{
  assert.equal(query,REVIEW_METADATA_QUERY);assert.equal(variables.owner,'example');assert.equal(variables.name,'repo');assert.equal(variables.number,7);
  assert.equal(variables.cursor,calls===0?null:'next');
  const connection={nodes:[rows[calls]],pageInfo:{hasNextPage:calls===0,endCursor:calls===0?'next':null}};calls++;
  return {repository:{pullRequest:{reviews:connection}}};
 }};
 assert.deepEqual(await readReviewMetadata({github,repository:'example/repo',number:7}),rows);assert.equal(calls,2);
 for(const page of [null,{nodes:[],pageInfo:{hasNextPage:true,endCursor:null}},{nodes:[],pageInfo:{hasNextPage:true,endCursor:'same'}}]) {
  github.graphql=async()=>({repository:{pullRequest:{reviews:page}}});
  await assert.rejects(readReviewMetadata({github,repository:'example/repo',number:7}),/GraphQL|cursor/);
 }
});

test('edited automated APPROVED concerns keep edit time instead of immutable approval submission',()=>{
 const finding=submitted('claude[bot]','APPROVED','<!-- gumnut-review-finding/v1 severity=significant -->\nPreserve the invariant.');
 const clean={id:20,user:finding.user,body:`<!-- gumnut-review-verdict/v1 head=${'a'.repeat(40)} base=${'b'.repeat(40)} verdict=clean -->`,created_at:'2026-10-14T00:00:02Z'};
 const graph=metadata(finding,{lastEditedAt:'2026-10-14T00:00:03Z',updatedAt:'2026-10-14T00:00:03Z'});
 assert.deepEqual(pendingReviewBodies([finding],[clean],[graph]).map(item=>item.id),[finding.id]);
});

test('review authority is shared across REST and GraphQL and excludes publisher and unrelated bots',()=>{
 const {trustedReviewer}=require('./review-feedback.cjs');
 for(const login of ['claude','chatgpt-codex-connector','gumnut-reviewer']) {
  assert(trustedReviewer({login:`${login}[bot]`,type:'Bot'},'NONE'));
  assert(trustedReviewer({login,__typename:'Bot'},'NONE'));
 }
 for(const association of ['OWNER','MEMBER','COLLABORATOR']) {
  assert(trustedReviewer({login:'maintainer',type:'User'},association));
  assert(trustedReviewer({login:'maintainer',__typename:'User'},association));
 }
 for(const [user,association] of [[{login:'outside',type:'User'},'NONE'],[{login:'unrelated[bot]',type:'Bot'},'MEMBER'],[{login:'gumnut-bot[bot]',type:'Bot'},'MEMBER'],[{login:'gumnut-bot[bot]',type:'User'},'MEMBER'],[{login:'gumnut-bot',__typename:'Bot'},'OWNER']])
  assert.equal(trustedReviewer(user,association),false);
});
test('only trusted formal reviews including changes requests and trusted conversation comments enter pending work',()=>{
 const trusted=review('maintainer','CHANGES_REQUESTED','Please fix the invariant.');
 assert.deepEqual(pendingReviewBodies([trusted]),[trusted]);
 for(const outsider of [{...trusted,author_association:'NONE'},{...trusted,user:{login:'unrelated[bot]',type:'Bot'}},{...trusted,user:{login:'gumnut-bot[bot]',type:'Bot'}}]) {
  assert.deepEqual(pendingReviewBodies([outsider]),[]);
  assert.deepEqual(pendingReviewBodies([{...outsider,state:'COMMENTED'}]),[]);
  assert.deepEqual(pendingReviewBodies([],[{...outsider,created_at:trusted.submitted_at}]),[]);
 }
 const untrustedClear={...trusted,id:2,state:'APPROVED',author_association:'NONE',submitted_at:'2026-10-14T00:01:00Z'};
 assert.deepEqual(pendingReviewBodies([trusted,untrustedClear]),[trusted]);
 assert.deepEqual(pendingReviewBodies([trusted,{...untrustedClear,author_association:'MEMBER'}]),[]);
});
test('thread authority comes from the original author and is shared by the pure CLI',()=>{
 const {trustedReviewThreads}=require('./review-feedback.cjs');
 const comment=(login,type,association)=>({id:login,body:'Please preserve the invariant.',author:{login,__typename:type},authorAssociation:association});
 const thread=(id,first,reply)=>({id,isResolved:false,comments:{nodes:[first,...(reply?[reply]:[])],pageInfo:{hasNextPage:false}}});
 const human=comment('maintainer','User','MEMBER'),outsider=comment('outside','User','NONE');
 const trusted=thread('trusted',human,outsider),ignored=thread('ignored',outsider,human);
 const publisher=thread('publisher',comment('gumnut-bot','Bot','MEMBER'),human);
 const projected={...trusted,comments:{...trusted.comments,nodes:[human]}};
 assert.deepEqual(trustedReviewThreads([ignored,publisher,trusted]),[projected]);
 assert.equal(trusted.comments.nodes.length,2);
 assert.deepEqual(trustedReviewThreads([{...trusted,isResolved:true}]),[]);
 assert.throws(()=>trustedReviewThreads([{...trusted,comments:{nodes:[{body:'missing authority'}],pageInfo:{hasNextPage:false}}}]),/author evidence/);
 assert.throws(()=>trustedReviewThreads([{...trusted,comments:{...trusted.comments,pageInfo:{hasNextPage:true}}}]),/thread evidence/);
 const {execFileSync}=require('node:child_process');
 const output=execFileSync(process.execPath,[require.resolve('./review-feedback.cjs')],{input:JSON.stringify({reviews:[],comments:[],metadata:[],threads:[ignored,trusted,publisher]}),encoding:'utf8'});
 assert.deepEqual(JSON.parse(output),{bodies:[],threads:[projected]});
});

test('linked same-Bot Codex advisory inline findings classify a review wrapper as clean',()=>{
 const wrapper=submitted('chatgpt-codex-connector[bot]','COMMENTED',`Codex Review: Here are some suggestions.\n\n**Reviewed commit:** \`${'a'.repeat(40)}\``);
 const linked={id:50,pull_request_review_id:wrapper.id,user:wrapper.user,body:'[P3] Consider clearer wording.'};
 const graph=[metadata(wrapper)];
 assert.deepEqual(pendingReviewBodies([wrapper],[],graph,[linked]),[]);
 for(const findings of [[],[{...linked,pull_request_review_id:99}],[{...linked,user:{login:'claude[bot]',type:'Bot'}}],
   [{...linked,user:{...wrapper.user,type:'User'}}],[{...linked,body:'[P1] Preserve data'}],
   [linked,{...linked,id:51,body:'[P1] Preserve data'}],[{...linked,body:'Please investigate this unclassified concern.'}]])
   assert.deepEqual(pendingReviewBodies([wrapper],[],graph,findings).map(item=>item.id),[wrapper.id]);
 const thread={id:'unresolved',isResolved:false,comments:{nodes:[{author:{login:'maintainer',__typename:'User'},authorAssociation:'MEMBER',body:'Preserve this invariant.'}],pageInfo:{hasNextPage:false}}};
 const {execFileSync}=require('node:child_process');
 const output=execFileSync(process.execPath,[require.resolve('./review-feedback.cjs')],{input:JSON.stringify({reviews:[wrapper],comments:[],metadata:graph,findings:[linked],threads:[thread]}),encoding:'utf8'});
 assert.deepEqual(JSON.parse(output),{bodies:[],threads:[thread]});
});

function resolvedEvidence(severity='P1') {
 const wrapper=submitted('chatgpt-codex-connector[bot]','COMMENTED',`Codex Review: Here are some suggestions.\n\n**Reviewed commit:** \`${'a'.repeat(40)}\``,10);
 const finding={id:50,node_id:'inline-node',pull_request_review_id:wrapper.id,user:wrapper.user,body:`[${severity}] Preserve the invariant.`};
 const thread={id:'resolved-thread',isResolved:true,comments:{pageInfo:{hasNextPage:false},nodes:[{
  id:finding.node_id,body:finding.body,authorAssociation:'NONE',
  author:{login:'chatgpt-codex-connector',__typename:'Bot'},pullRequestReview:{databaseId:wrapper.id},
 }]}};
 return {wrapper,finding,thread};
}
test('complete resolved inline proof removes only canonical COMMENTED concern wrappers, including the CLI',()=>{
 for(const severity of ['P1','P2']) {
  const {wrapper,finding,thread}=resolvedEvidence(severity);
  assert.deepEqual(pendingReviewBodies([wrapper],[],[metadata(wrapper)],[finding],[thread]),[]);
  const {execFileSync}=require('node:child_process');
  const output=execFileSync(process.execPath,[require.resolve('./review-feedback.cjs')],{input:JSON.stringify({reviews:[wrapper],comments:[],metadata:[metadata(wrapper)],findings:[finding],threads:[thread]}),encoding:'utf8'});
  assert.deepEqual(JSON.parse(output),{bodies:[],threads:[]});
 }
});
test('missing partial duplicate stale or unauthenticated resolution proof retains the concern',()=>{
 const base=resolvedEvidence();
 const clone=()=>JSON.parse(JSON.stringify(base));
 const cases=[
  data=>data.thread.isResolved=false,
  data=>data.thread.comments.pageInfo.hasNextPage=true,
  data=>delete data.thread.comments.pageInfo,
  data=>data.thread.comments.nodes=[],
  data=>delete data.finding.node_id,
  data=>data.thread.comments.nodes[0].id='different-node',
  data=>data.thread.comments.nodes[0].author.__typename='User',
  data=>data.thread.comments.nodes[0].author.login='claude',
  data=>data.thread.comments.nodes[0].body+=' edited',
  data=>data.thread.comments.nodes[0].pullRequestReview.databaseId=99,
  data=>delete data.thread.comments.nodes[0].pullRequestReview,
  data=>data.finding.pull_request_review_id=99,
 ];
 for(const mutate of cases) {
  const data=clone();mutate(data);
  assert.deepEqual(pendingReviewBodies([data.wrapper],[],[metadata(data.wrapper)],[data.finding],[data.thread]).map(item=>item.id),[data.wrapper.id]);
 }
 const {wrapper,finding,thread}=base;
 for(const threads of [[],[thread,thread],[{...thread,comments:{...thread.comments,nodes:[...thread.comments.nodes,...thread.comments.nodes]}}],[thread,{...thread,id:'truncated',comments:{nodes:[],pageInfo:{hasNextPage:true}}}]])
  assert.deepEqual(pendingReviewBodies([wrapper],[],[metadata(wrapper)],[finding],threads).map(item=>item.id),[wrapper.id]);
 assert.deepEqual(pendingReviewBodies([wrapper],[],[metadata(wrapper)],[finding,finding],[thread]).map(item=>item.id),[wrapper.id]);
 const second={...finding,id:51,node_id:'second-node'};
 assert.deepEqual(pendingReviewBodies([wrapper],[],[metadata(wrapper)],[finding,second],[thread]).map(item=>item.id),[wrapper.id]);
});
test('resolved wrappers cannot clear independent body findings or formal changes requests',()=>{
 const {wrapper,finding,thread}=resolvedEvidence();
 const older=submitted(wrapper.user.login,'COMMENTED','[P1] Keep this independent body concern.',1);
 const result=pendingReviewBodies([older,wrapper],[],[metadata(older),metadata(wrapper)],[finding],[thread]);
 assert.deepEqual(result.map(item=>item.id),[older.id]);
 const formal={...wrapper,state:'CHANGES_REQUESTED'};
 assert.deepEqual(pendingReviewBodies([formal],[],[metadata(formal)],[finding],[thread]).map(item=>item.id),[formal.id]);
 const mixed={...wrapper,body:wrapper.body+'\n\n[P2] Also preserve this body invariant.'};
 assert.deepEqual(pendingReviewBodies([mixed],[],[metadata(mixed)],[finding],[thread]).map(item=>item.id),[mixed.id]);
 const approved={...wrapper,state:'APPROVED'};
 assert.deepEqual(pendingReviewBodies([approved],[],[metadata(approved)],[finding],[thread]).map(item=>item.id),[approved.id]);
});
test('resolution proof cannot discard a linked REST finding with a different author or actor type',()=>{
 const {wrapper,finding,thread}=resolvedEvidence();
 for(const user of [{login:'claude[bot]',type:'Bot'},{login:wrapper.user.login,type:'User'}]) {
  const other={...finding,id:51,node_id:'other-inline',user};
  const proof={...thread,comments:{...thread.comments,nodes:[...thread.comments.nodes,
   {...thread.comments.nodes[0],id:other.node_id},
  ]}};
  assert.deepEqual(pendingReviewBodies([wrapper],[],[metadata(wrapper)],[finding,other],[proof]).map(item=>item.id),[wrapper.id]);
 }
});
test('thread projection clones containers and keeps only trusted replies without mutating raw proof',()=>{
 const {trustedReviewThreads}=require('./review-feedback.cjs');
 const comment=(login,type,association)=>({id:login,body:'Feedback',author:{login,__typename:type},authorAssociation:association});
 const original=comment('maintainer','User','MEMBER'),outside=comment('outside','User','NONE'),bot=comment('unrelated','Bot','NONE'),trusted=comment('claude','Bot','NONE');
 const raw={id:'trusted',isResolved:false,comments:{pageInfo:{hasNextPage:false},nodes:[original,outside,bot,trusted]}};
 const snapshot=JSON.stringify(raw),projection=trustedReviewThreads([raw]);
 assert.deepEqual(projection[0].comments.nodes,[original,trusted]);
 assert.notEqual(projection[0],raw);assert.notEqual(projection[0].comments,raw.comments);assert.notEqual(projection[0].comments.nodes,raw.comments.nodes);
 assert.equal(JSON.stringify(raw),snapshot);
 assert.deepEqual(trustedReviewThreads([{...raw,comments:{...raw.comments,nodes:[outside,original,trusted]}}]),[]);
});
