// Trusted dispatch-source inventory and admission for native daemon PR follow-up.
const {pendingReviewBodies,readReviewMetadata,trustedReviewThreads} = require('./review-feedback.cjs');
const BOT = "gumnut-bot[bot]";
const POLICIES = Object.freeze({
  librarian: {prefix: "librarian/", label: "librarian"},
  maintainer: {prefix: "maintainer/", label: "maintainer"},
});
function policyFor(role) {
  const policy = POLICIES[role];
  if (!policy) throw Error("Unknown follow-up role");
  return policy;
}
function isOwned(pr, repository, role) {
  const policy = policyFor(role);
  return pr.state === "open" && !pr.draft && pr.user?.login === BOT &&
    pr.head?.repo?.full_name === repository && pr.base?.repo?.full_name === repository &&
    pr.base.ref === "main" && pr.head.ref.startsWith(policy.prefix) &&
    pr.labels?.some(label => label.name === policy.label) &&
    /^[a-f0-9]{40}$/.test(pr.head.sha) && /^[a-f0-9]{40}$/.test(pr.base.sha);
}
async function readFeedback(github, owner, repo, number) {
  const feedback = [], rawThreads=[];
  let cursor = null;
  do {
    const result = await github.graphql(`query($owner:String!,$repo:String!,$number:Int!,$cursor:String) {
      repository(owner:$owner,name:$repo) { pullRequest(number:$number) {
        reviewThreads(first:100,after:$cursor) { pageInfo {hasNextPage endCursor}
          nodes {id isResolved isOutdated path line comments(first:100) {pageInfo {hasNextPage} nodes {id body url createdAt authorAssociation pullRequestReview {databaseId} author {login __typename}}}}
        }
      }}
    }`, {owner, repo, number, cursor});
    const page = result.repository?.pullRequest?.reviewThreads;
    if (!page || !Array.isArray(page.nodes) || typeof page.pageInfo?.hasNextPage !== "boolean") throw Error("Incomplete review-thread inventory");
    rawThreads.push(...page.nodes);
    for (const thread of trustedReviewThreads(page.nodes)) {
      const comments=thread.comments.nodes;
      feedback.push({kind:"thread", thread:thread.id, outdated:thread.isOutdated,
        path:thread.path, line:thread.line, comments});
    }
    if (page.pageInfo.hasNextPage && (!page.pageInfo.endCursor || page.pageInfo.endCursor === cursor))
      throw Error("Review-thread pagination did not advance");
    cursor = page.pageInfo.hasNextPage ? page.pageInfo.endCursor : null;
  } while (cursor);
  const reviews = await github.paginate(github.rest.pulls.listReviews,
    {owner, repo, pull_number:number, per_page:100});
  const comments = await github.paginate(github.rest.issues.listComments,
    {owner, repo, issue_number:number, per_page:100});
  const findings=await github.paginate(github.rest.pulls.listReviewComments,
    {owner, repo, pull_number:number, per_page:100});
  const metadata=await readReviewMetadata({github,repository:`${owner}/${repo}`,number});
  for (const review of pendingReviewBodies(reviews, comments,metadata,findings,rawThreads)) {
    feedback.push({kind:'review', id:review.node_id, body:review.body || '',
      url:review.html_url, author:{login:review.user.login},
      createdAt:review.submitted_at || review.created_at, commit:review.commit_id});
  }
  return feedback;
}
async function collectFollowUp({github, context, role}) {
  policyFor(role);
  const {owner, repo} = context.repo, repository = `${owner}/${repo}`;
  const prs = await github.paginate(github.rest.pulls.list,
    {owner, repo, state:"open", per_page:100});
  const owned = prs.filter(pr => isOwned(pr, repository, role))
    .sort((a,b) => a.created_at.localeCompare(b.created_at) || a.number - b.number);
  let target = null;
  for (const pr of owned) {
    const feedback = await readFeedback(github, owner, repo, pr.number);
    if (!feedback.length) continue;
    const requested = (await github.rest.pulls.listRequestedReviewers(
      {owner, repo, pull_number:pr.number})).data;
    target = {number:pr.number, head:pr.head.sha, base:pr.base.sha, branch:pr.head.ref,
      url:pr.html_url, feedback, reviewers:requested.users.map(user=>user.login),
      teams:requested.teams.map(team=>team.slug)};
    break;
  }
  return {source:context.sha, repository, role, target};
}
function validateManifest(manifest, context, role) {
  const repository = `${context.repo.owner}/${context.repo.repo}`;
  policyFor(role);
  if (manifest?.source !== context.sha || manifest.repository !== repository || manifest.role !== role)
    throw Error("Follow-up manifest does not match dispatch");
  const target = manifest.target;
  if (target !== null && (!target || !Number.isSafeInteger(target.number) || target.number < 1 ||
      !/^[a-f0-9]{40}$/.test(target.head) || !/^[a-f0-9]{40}$/.test(target.base) ||
      !target.branch?.startsWith(policyFor(role).prefix) || !Array.isArray(target.feedback) || !target.feedback.length))
    throw Error("Invalid admitted follow-up target");
  return target;
}
async function guardFollowUp({github, context, role, output, manifest}) {
  const target = validateManifest(manifest, context, role);
  if (!Array.isArray(output?.errors) || output.errors.length || !Array.isArray(output.items) ||
      output.items.some(item=>!item || typeof item !== "object"))
    throw Error("Invalid native output envelope");
  const items = output.items;
  if (items.length === 1 && items[0].type === "noop") return "noop";
  if (!target) {
    if (items.some(item=>item.type !== "create_pull_request" && item.type !== "add_reviewer") ||
        items.filter(item=>item.type === "create_pull_request").length !== 1)
      throw Error("Fresh discovery must create one PR or noop");
    // Catch feedback that arrived after discovery, before a fresh PR can publish.
    if ((await collectFollowUp({github, context, role})).target)
      throw Error("Native review feedback now has priority over a new PR");
    return "proposal";
  }
  if (items.length !== 1 || items[0].type !== "push_to_pull_request_branch" ||
      !/^[1-9]\d*$/.test(String(items[0].pull_request_number)) ||
      Number(items[0].pull_request_number) !== target.number ||
      (items[0].repo && items[0].repo !== manifest.repository))
    throw Error("Follow-up must update exactly the admitted PR, or noop");
  const {owner, repo} = context.repo;
  const pr = (await github.rest.pulls.get({owner, repo, pull_number:target.number})).data;
  if (!isOwned(pr, manifest.repository, role) || pr.head.sha !== target.head ||
      pr.base.sha !== target.base || pr.head.ref !== target.branch)
    throw Error("Admitted PR identity or head/base changed");
  return "follow-up";
}
module.exports = {BOT, POLICIES, policyFor, isOwned, readFeedback, collectFollowUp,
  validateManifest, guardFollowUp};
