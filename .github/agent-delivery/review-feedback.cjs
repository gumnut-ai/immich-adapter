// Reuse reviewer-owned body formats and ordering across reviews and PR comments.
const {commentDecision, latestActiveFormal, latestReviewDecision, reviewMetadata, joinReviewMetadata, REVIEW_METADATA_QUERY} = require('../pr-approval/coordinator.cjs');
const {bodyBlocking, isReviewCommand} = require('../pr-review/reviewer.cjs');
const AUTOMATED = new Set(['claude[bot]', 'chatgpt-codex-connector[bot]']);
const acknowledgement = body => /^(?:(?:looks? (?:good|great)(?: to me)?|lgtm|thanks?(?: you)?|approved?|nice(?: work)?|all good|resolved|done|fixed|addressed)[.,!\s👍✅]*)+$/iu.test(body.trim());
// Accept REST users and GraphQL actors; only genuine Bot actors receive the
// GraphQL-to-REST suffix normalization. Publisher bots are not reviewers.
function trustedReviewer(user, association) {
  const type=user?.type || user?.__typename;
  const login=type==='Bot' && user?.login && !user.login.endsWith('[bot]') ? `${user.login}[bot]` : user?.login;
  if(login==='gumnut-bot[bot]') return false;
  return type==='Bot' && (AUTOMATED.has(login) || login==='gumnut-reviewer[bot]') ||
    type==='User' && ['OWNER','MEMBER','COLLABORATOR'].includes(association);
}
function trustedReviewThreads(threads) {
  if(!Array.isArray(threads)) throw new Error('Review thread rows required');
  return threads.filter(thread=>{
    if(thread.isResolved===true) return false;
    if(thread.isResolved!==false || !Array.isArray(thread.comments?.nodes) || !thread.comments.nodes.length)
      throw new Error('Incomplete unresolved thread evidence');
    const original=thread.comments.nodes[0];
    if(typeof original.authorAssociation!=='string' || (original.author!==null && (!original.author?.login || !original.author?.__typename)))
      throw new Error('Incomplete original thread author evidence');
    if(!trustedReviewer(original.author,original.authorAssociation)) return false;
    if(thread.comments.pageInfo?.hasNextPage!==false) throw new Error('Incomplete unresolved thread evidence');
    return true;
  });
}
// Human approval clears that author's prose findings, not native thread or
// formal changes-requested state. Commands and acknowledgements are not work.
function humanDecision(body) {
  const text = body.trim();
  if (isReviewCommand(text) || /^@codex (?:security )?review(?:[ \t]+<!-- gumnut-codex-head:[a-f0-9]{40} -->)?$/i.test(text)) return null;
  if (acknowledgement(text)) {
    return /\b(?:looks? (?:good|great)|lgtm|approve(?:d)?|all good|resolved|done|fixed|addressed)\b/i.test(text) ? {state:'clean'} : null;
  }
  return {state:'concerns'};
}
// Node collectors use the coordinator's paginated query. Python collectors can
// fetch REVIEW_METADATA_QUERY themselves and pass its nodes to the pure CLI.
async function readReviewMetadata({github, repository, number}) {
  const api = async (_method, _route, {query,variables}) => ({data:await github.graphql(query,variables)});
  return [...(await reviewMetadata(api, repository, number)).values()];
}
function pendingReviewBodies(reviews, comments=[], metadata, findings=[]) {
  if (!Array.isArray(findings)) throw new Error('Inline review findings required');
  reviews=reviews.filter(item=>trustedReviewer(item.user,item.author_association));
  if (metadata !== undefined) {
    if (!Array.isArray(metadata)) throw new Error('Review metadata rows required');
    const byId = new Map();
    for (const row of metadata) {
      if (!Number.isSafeInteger(row.databaseId) || row.databaseId<=0 || byId.has(row.databaseId)) throw new Error('Invalid or duplicate review metadata ID');
      byId.set(row.databaseId,row);
    }
    reviews=joinReviewMetadata(reviews,byId,'gumnut-reviewer[bot]',{requireAll:true});
  }
  const formal = latestActiveFormal(reviews), byAuthor = new Map();
  const events = reviews.map(item=>({item,kind:'review'})).concat(
    comments.filter(item=>trustedReviewer(item.user,item.author_association))
      .map(item=>({item,kind:'comment'})));
  for (const {item,kind} of events) {
    const login = item.user?.login;
    if (!login || item.state === 'PENDING' || item.state === 'DISMISSED') continue;
    let verdict;
    // Formal changes requests are retained independently below until approved.
    if (item.state === 'CHANGES_REQUESTED') continue;
    if (AUTOMATED.has(login)) {
      const associatedFindings = kind === 'review' && item.user?.type === 'Bot'
        ? findings.filter(comment=>comment.pull_request_review_id === item.id &&
            comment.user?.type === 'Bot' && comment.user.login === login) : [];
      verdict = commentDecision(item, login, kind, associatedFindings);
      if (!verdict && item.state === 'APPROVED') verdict = {state:'clean'};
    } else if (kind === 'review' && item.state === 'APPROVED') verdict = {state:'clean'};
    else if (item.body?.trim() && (kind === 'comment' || item.state === 'COMMENTED')) {
      verdict = login === 'gumnut-reviewer[bot]'
        ? (bodyBlocking(item.body) ? {state:'concerns'} : null)
        : humanDecision(item.body);
    }
    if (!verdict) continue;
    if (!byAuthor.has(login)) byAuthor.set(login, []);
    byAuthor.get(login).push({...item, ...(kind === 'review' && item.state === 'APPROVED' && verdict.state === 'clean' ? {decision_at:item.submitted_at} : {}), verdict, evidence:kind === 'comment' ? {...item, review_feedback_kind:'comment'} : item});
  }
  const bodies = [...byAuthor.values()].flatMap(events=>latestReviewDecision(events).pending.map(event=>event.evidence));
  return [...new Map([...formal.values()].filter(item=>item.state === 'CHANGES_REQUESTED')
    .concat(bodies).map(item=>[`${item.review_feedback_kind || 'review'}:${item.id || item.node_id}`, item])).values()];
}
if (require.main === module) {
  const fs=require('node:fs');
  const input=JSON.parse(fs.readFileSync(0,'utf8'));
  const bodies=pendingReviewBodies(input.reviews,input.comments,input.metadata,input.findings);
  process.stdout.write(JSON.stringify(Object.hasOwn(input,'threads') ? {bodies,threads:trustedReviewThreads(input.threads)} : bodies));
}
module.exports = {pendingReviewBodies,readReviewMetadata,REVIEW_METADATA_QUERY,trustedReviewer,trustedReviewThreads};
