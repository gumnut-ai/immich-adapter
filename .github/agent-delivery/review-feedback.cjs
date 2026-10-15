// Reuse reviewer-owned body formats and ordering across reviews and PR comments.
const {commentDecision, latestActiveFormal, latestReviewDecision} = require('../pr-approval/coordinator.cjs');
const {bodyBlocking} = require('../pr-review/reviewer.cjs');
const AUTOMATED = new Set(['claude[bot]', 'chatgpt-codex-connector[bot]']);
const acknowledgement = body => /^(?:(?:looks? (?:good|great)(?: to me)?|lgtm|thanks?(?: you)?|approved?|nice(?: work)?|all good)[.!\s👍✅]*)+$/iu.test(body.trim());
function pendingReviewBodies(reviews, comments=[]) {
  const formal = latestActiveFormal(reviews), byAuthor = new Map();
  const events = reviews.map(item=>({item,kind:'review'})).concat(
    comments.filter(item=>AUTOMATED.has(item.user?.login) || item.user?.login === 'gumnut-reviewer[bot]')
      .map(item=>({item,kind:'comment'})));
  for (const {item,kind} of events) {
    const login = item.user?.login;
    if (!login || item.state === 'PENDING' || item.state === 'DISMISSED') continue;
    let verdict;
    // Formal changes requests are retained independently below until approved.
    if (item.state === 'CHANGES_REQUESTED') continue;
    if (AUTOMATED.has(login)) {
      verdict = commentDecision(item, login, kind);
      if (!verdict && item.state === 'APPROVED') verdict = {state:'clean'};
    } else if (kind === 'review' && item.state === 'APPROVED') verdict = {state:'clean'};
    else if (item.body?.trim() && (kind === 'comment' || item.state === 'COMMENTED')) {
      if (login === 'gumnut-reviewer[bot]' ? bodyBlocking(item.body) : !acknowledgement(item.body)) verdict = {state:'concerns'};
    }
    if (!verdict) continue;
    if (!byAuthor.has(login)) byAuthor.set(login, []);
    byAuthor.get(login).push({...item, verdict, evidence:kind === 'comment' ? {...item, review_feedback_kind:'comment'} : item});
  }
  const bodies = [...byAuthor.values()].flatMap(events=>latestReviewDecision(events).pending.map(event=>event.evidence));
  return [...new Map([...formal.values()].filter(item=>item.state === 'CHANGES_REQUESTED')
    .concat(bodies).map(item=>[`${item.review_feedback_kind || 'review'}:${item.id || item.node_id}`, item])).values()];
}
if (require.main === module) {
  const fs=require('node:fs');
  const {reviews,comments}=JSON.parse(fs.readFileSync(0,'utf8'));
  process.stdout.write(JSON.stringify(pendingReviewBodies(reviews,comments)));
}
module.exports = {pendingReviewBodies};
