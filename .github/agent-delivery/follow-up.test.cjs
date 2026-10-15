const {test}=require('node:test');
const assert=require('node:assert/strict');
const {isOwned,collectFollowUp,guardFollowUp}=require('./follow-up.cjs');
const sha='a'.repeat(40),head='b'.repeat(40);
const context={sha,repo:{owner:'example',repo:'adapter'}};
function pr(number=1,role='librarian') {return {number,state:'open',draft:false,user:{login:'gumnut-bot[bot]'},created_at:`2026-10-${String(number).padStart(2,'0')}`,labels:[{name:role}],head:{sha:head,ref:`${role}/topic`,repo:{full_name:'example/adapter'}},base:{sha,ref:'main',repo:{full_name:'example/adapter'}},html_url:'https://example.com/pr'};}
function fixture(rows=[pr()]) {
 const calls=[];
 const github={rest:{issues:{listComments:'comments'},pulls:{list:'list',listReviews:'reviews',get:async()=>({data:rows[0]}),listRequestedReviewers:async()=>({data:{users:[{login:'human'}],teams:[]}})}},
 paginate:async(method)=>method==='list'?rows:[],
 graphql:async args=>{calls.push(args);return {repository:{pullRequest:{reviewThreads:{nodes:[{id:'thread',isResolved:false,isOutdated:false,path:'docs/a.md',comments:{pageInfo:{hasNextPage:false},nodes:[{id:'comment',body:'Fix this',createdAt:'2026-10-14'}]}}],pageInfo:{hasNextPage:false}}}}};}};
 return {github,calls};
}
test('admission requires exact native author repository base role and label',()=>{
 assert(isOwned(pr(),'example/adapter','librarian'));
 for(const mutate of [p=>p.user.login='CharlieHelps',p=>p.head.repo.full_name='other/repo',p=>p.base.ref='develop',p=>p.head.ref='maintainer/topic',p=>p.labels=[],p=>p.state='closed',p=>p.draft=true]) {
  const p=pr();mutate(p);assert.equal(isOwned(p,'example/adapter','librarian'),false);
 }
});
test('oldest pending PR is deterministic and retains human requests',async()=>{
 const f=fixture([pr(2),pr(1)]),manifest=await collectFollowUp({github:f.github,context,role:'librarian'});
 assert.equal(manifest.target.number,1);assert.equal(manifest.target.feedback[0].comments[0].body,'Fix this');assert.deepEqual(manifest.target.reviewers,['human']);
});
test('selected feedback suppresses a second PR and only allows exact push or deferral',async()=>{
 const f=fixture(),manifest=await collectFollowUp({github:f.github,context,role:'librarian'});
 const check=items=>guardFollowUp({github:f.github,context,role:'librarian',manifest,output:{errors:[],items}});
 assert.equal(await check([{type:'noop'}]),'noop');
 assert.equal(await check([{type:'push_to_pull_request_branch',pull_request_number:1}]),'follow-up');
 for(const items of [[{type:'create_pull_request'}],[{type:'push_to_pull_request_branch',pull_request_number:2}],[{type:'noop'},{type:'push_to_pull_request_branch',pull_request_number:1}]]) await assert.rejects(check(items));
 f.github.rest.pulls.get=async()=>({data:{...pr(),head:{...pr().head,sha:'c'.repeat(40)}}});
 await assert.rejects(check([{type:'push_to_pull_request_branch',pull_request_number:1}]),/changed/);
});
test('new discovery is blocked by feedback arriving before publication',async()=>{
 const f=fixture();
 await assert.rejects(guardFollowUp({github:f.github,context,role:'librarian',manifest:{source:sha,repository:'example/adapter',role:'librarian',target:null},output:{errors:[],items:[{type:'create_pull_request'}]}}),/priority/);
});
