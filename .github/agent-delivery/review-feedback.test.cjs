const {test} = require('node:test');
const assert = require('node:assert/strict');
const {pendingReviewBodies} = require('./review-feedback.cjs');
const review = (login,state,body,id=1) => ({id,node_id:String(id),user:{login},state,body,submitted_at:`2026-10-14T00:00:${String(id).padStart(2,'0')}Z`});
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
  const concern={id:5,user:{login:'claude[bot]'},body:'<!-- gumnut-review-finding/v1 severity=significant -->\nFix lost updates.',created_at:'2026-10-14T00:00:00Z'};
  const clean={id:6,user:{login:'claude[bot]'},body:`<!-- gumnut-review-verdict/v1 head=${'a'.repeat(40)} base=${'b'.repeat(40)} verdict=clean -->`,created_at:'2026-10-14T00:01:00Z'};
  assert.equal(pendingReviewBodies([], [concern]).length,1);
  assert.deepEqual(pendingReviewBodies([], [concern,clean]),[]);
  const notice={id:7,user:{login:'chatgpt-codex-connector[bot]'},body:`<!-- codex-pull-request-review-summary -->\n## Codex Review Summary\nThis comment shows the latest Codex review activity on this pull request.\n| Review | Status | Commit | Review trigger |\n| --- | --- | --- | --- |\n| 📝 **Code Review** | ✅ **Completed** | \`aaaaaaa\` | Manual request |`};
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
