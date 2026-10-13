// Trusted default-branch code only; contributor code is never loaded.
const CLAUDE = 'claude[bot]';
const CODEX = 'chatgpt-codex-connector[bot]';
const MARKER = '<!-- gumnut-pr-approval/v1 -->';
const sha = value => /^[0-9a-f]{40}$/.test(value || '');
const positive = value => /^[1-9][0-9]*$/.test(String(value)) && Number.isSafeInteger(Number(value));
const unwrap = response => response?.data ?? response;
const eventTime = item => Date.parse(item.decision_at || item.submitted_at || item.updated_at || item.created_at);
const ordered = items => [...items].sort((a, b) => eventTime(a) - eventTime(b) || Number(a.id) - Number(b.id));
const timestamp = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(value) && Number.isFinite(Date.parse(value));
const restLogin = author => author?.__typename === 'Bot' && !author.login?.endsWith('[bot]') ? `${author.login}[bot]` : author?.login;
const significant = (body, login) => login === CLAUDE ? /<!-- gumnut-review-finding\/v1 severity=significant -->|should be fixed before merge/i.test(body || '') : login === CODEX && /\bP[012]\b/.test(body || '');
const advisory = (body, login) => login === CLAUDE ? /^(?:\*\*)?Claude review[^\n]*\badvisory\b/i.test(body || '') : login === CODEX && /^(?:(?:\*\*)?\[P3\](?:\s|$)|\*\*<sub><sub>!\[P3 Badge\]\(https:\/\/img\.shields\.io\/badge\/P3-[^()\s]+\)<\/sub><\/sub>\s)/.test(body || '') && !significant(body, login);
const normalizeSpace = text => text.replace(/\s+/g, ' ').trim();
// Observed hosted output, deliberately narrow. Changed boilerplate is unknown
// evidence until verified; it must not hide appended substantive feedback.
const codexFooters = [
  `<details> <summary>ℹ️ About Codex in GitHub</summary> <br/> [Your team has set up Codex to review pull requests in this repo](https://chatgpt.com/codex/cloud/settings/general). Reviews are triggered when you - Open a pull request for review - Mark a draft as ready - Comment "@codex review". If Codex has suggestions, it will comment; otherwise it will react with 👍. Codex can also answer questions or update the PR. Try commenting "@codex address that feedback". </details>`,
  `<details> <summary>ℹ️ About Codex in GitHub</summary> <br/> [Your team has set up Codex to review pull requests in this repo](https://chatgpt.com/codex/cloud/settings/general). Reviews are triggered when you - Open a pull request for review - Mark a draft as ready - Comment "@codex review" or "@codex security review". Codex reacts with 👀 while any review is running, comments if it has suggestions, and reacts with 👍 once all reviews finish with no findings. </details>`,
].map(normalizeSpace);
function codexContent(body) {
  const start = body.indexOf('<details>');
  if (start < 0) return body.trim();
  return codexFooters.includes(normalizeSpace(body.slice(start))) ? body.slice(0, start).trim() : null;
}
function codexActivity(body) {
  const lines = body?.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  if (!lines || lines.length < 6 || lines[0] !== '<!-- codex-pull-request-review-summary -->' || lines[1] !== '## Codex Review Summary' || lines[2] !== 'This comment shows the latest Codex review activity on this pull request.' || lines[3] !== '| Review | Status | Commit | Review trigger |' || lines[4] !== '| --- | --- | --- | --- |') return false;
  return lines.slice(5).every(line => /^\| 📝 \*\*Code Review\*\* \| (?:✅|🔄|👀|⏳|❌|⚠️) \*\*(?:Completed|Running|Queued|Failed|Cancelled)\*\*(?: (?:since )?<relative-time datetime="(\d{4}-\d{2}-\d{2}T[0-9:.]+Z)">\1<\/relative-time>)? \| `[0-9a-f]{7,40}` \| (?:Manual request|Pull request opened|PR opened|Automatic review) \|$/.test(line));
}

function outsideFences(body) {
  const lines = [];
  let fence;
  for (const line of body.split(/\r?\n/)) {
    const delimiter = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (fence) {
      if (delimiter && delimiter[1][0] === fence[0] && delimiter[1].length >= fence.length && /^[ \t]*$/.test(delimiter[2])) fence = undefined;
    } else if (delimiter && (delimiter[1][0] === '~' || !delimiter[2].includes('`'))) fence = delimiter[1];
    else lines.push(line);
  }
  return lines.join('\n');
}

function validate({ repo, number, bot }) {
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo || '') || !positive(number) || !/^[A-Za-z0-9-]+\[bot\]$/.test(bot || '')) throw new Error('Invalid coordinator target or bot identity');
}

// Explicit pages work with both REST clients and small offline fixtures. Never
// infer that a partial page whose advertised total is larger is complete.
async function pages(api, route, key) {
  const result = [];
  let total;
  for (let page = 1; page <= 1000; page++) {
    const data = unwrap(await api('GET', `${route}${route.includes('?') ? '&' : '?'}per_page=100&page=${page}`));
    const items = key ? data?.[key] : data;
    if (!Array.isArray(items)) throw new Error('Incomplete REST collection');
    if (key) {
      if (!Number.isSafeInteger(data.total_count) || data.total_count < 0) throw new Error('Missing REST collection total');
      if (total !== undefined && total !== data.total_count) throw new Error('REST collection changed during pagination');
      total = data.total_count;
    }
    result.push(...items);
    if (items.length < 100) {
      if (total !== undefined && total !== result.length) throw new Error('Truncated REST collection');
      return result;
    }
  }
  throw new Error('REST pagination limit exceeded');
}

function connection(value) {
  if (!value || !Array.isArray(value.nodes) || !value.pageInfo || typeof value.pageInfo.hasNextPage !== 'boolean' || value.nodes.some(node => !node)) throw new Error('Incomplete GraphQL collection');
  if (value.pageInfo.hasNextPage && !value.pageInfo.endCursor) throw new Error('Missing GraphQL cursor');
  return value;
}
async function graphql(api, query, variables) {
  const response = await api('POST', '/graphql', { query, variables });
  const data = response?.data?.data ? response.data : response;
  if (data?.errors?.length || !data?.data) throw new Error('GraphQL evidence unavailable');
  return data.data;
}
async function threads(api, repo, number) {
  const [owner, name] = repo.split('/');
  const query = `query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$cursor){nodes{id isResolved isOutdated comments(first:100){nodes{id body url createdAt pullRequestReview{databaseId} author{login __typename}} pageInfo{hasNextPage endCursor}}} pageInfo{hasNextPage endCursor}}}}}`;
  const commentQuery = `query($id:ID!,$cursor:String){node(id:$id){... on PullRequestReviewThread{comments(first:100,after:$cursor){nodes{id body url createdAt pullRequestReview{databaseId} author{login __typename}} pageInfo{hasNextPage endCursor}}}}}`;
  const result = [];
  let cursor = null;
  for (let page = 0; page < 1000; page++) {
    const data = await graphql(api, query, { owner, name, number: Number(number), cursor });
    const batch = connection(data.repository?.pullRequest?.reviewThreads);
    for (const thread of batch.nodes) {
      if (!thread.id || typeof thread.isResolved !== 'boolean') throw new Error('Incomplete thread metadata');
      let comments = connection(thread.comments);
      const nodes = [...comments.nodes];
      const seen = new Set();
      for (let c = 0; comments.pageInfo.hasNextPage; c++) {
        const next = comments.pageInfo.endCursor;
        if (c >= 1000 || seen.has(next)) throw new Error('Comment pagination limit exceeded');
        seen.add(next);
        comments = connection((await graphql(api, commentQuery, { id: thread.id, cursor: next })).node?.comments);
        nodes.push(...comments.nodes);
      }
      // GraphQL Bot.login omits REST's [bot] suffix. Normalize only genuine
      // Bot authors at this ingestion boundary; User names stay untouched.
      result.push({ ...thread, comments: nodes.map(c => c.author?.__typename === 'Bot' ? { ...c, author: { ...c.author, login: restLogin(c.author) } } : c) });
    }
    if (!batch.pageInfo.hasNextPage) return result;
    if (cursor === batch.pageInfo.endCursor) throw new Error('Repeated GraphQL cursor');
    cursor = batch.pageInfo.endCursor;
  }
  throw new Error('Thread pagination limit exceeded');
}

async function reviewMetadata(api, repo, number) {
  const [owner, name] = repo.split('/');
  const query = `query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){pullRequest(number:$number){reviews(first:100,after:$cursor){nodes{databaseId state body submittedAt updatedAt lastEditedAt commit{oid} author{login __typename}} pageInfo{hasNextPage endCursor}}}}}`;
  const result = new Map();
  const cursors = new Set();
  let cursor = null;
  for (let page = 0; page < 1000; page++) {
    const data = await graphql(api, query, { owner, name, number: Number(number), cursor });
    const batch = connection(data.repository?.pullRequest?.reviews);
    for (const review of batch.nodes) {
      if (!positive(review.databaseId) || result.has(Number(review.databaseId))) throw new Error('Incomplete or duplicate GraphQL review metadata');
      result.set(Number(review.databaseId), review);
    }
    if (!batch.pageInfo.hasNextPage) return result;
    cursor = batch.pageInfo.endCursor;
    if (cursors.has(cursor)) throw new Error('Repeated GraphQL review cursor');
    cursors.add(cursor);
  }
  throw new Error('Review metadata pagination limit exceeded');
}

function joinReviewMetadata(reviews, metadata, bot) {
  return reviews.map(review => {
    const source = [CLAUDE, CODEX, bot].includes(review.user?.login) && review.user?.type === 'Bot';
    const meta = metadata.get(Number(review.id));
    if (!meta) {
      if (source) throw new Error('Source review metadata unavailable');
      return review;
    }
    if (!timestamp(meta.updatedAt) || (meta.submittedAt !== null && !timestamp(meta.submittedAt)) || (meta.lastEditedAt !== null && !timestamp(meta.lastEditedAt))) throw new Error('Invalid GraphQL review timestamps');
    if (source && (meta.author?.__typename !== 'Bot' || restLogin(meta.author) !== review.user.login || !['APPROVED', 'CHANGES_REQUESTED', 'COMMENTED', 'DISMISSED', 'PENDING'].includes(review.state) || meta.state !== review.state || typeof review.body !== 'string' || meta.body !== review.body || !sha(review.commit_id) || meta.commit?.oid !== review.commit_id || (review.state !== 'PENDING' && !timestamp(meta.submittedAt)))) throw new Error('Source review metadata differs from REST evidence');
    // submittedAt is immutable. A COMMENTED concern changes when its body is
    // edited, so lastEditedAt provides the precise edit time. For dispositions,
    // updatedAt also captures state changes such as dismissal.
    const times = [meta.submittedAt, meta.lastEditedAt, ...(review.state === 'COMMENTED' ? [] : [meta.updatedAt])].filter(timestamp);
    if (!times.length) throw new Error('Review disposition timestamp unavailable');
    return { ...review, decision_at: new Date(Math.max(...times.map(Date.parse))).toISOString() };
  });
}

function latestWorkflowRuns(runs, head) {
  const latest = new Map();
  for (const run of runs) {
    if (!positive(run.id) || !positive(run.workflow_id) || !positive(run.run_attempt) || !timestamp(run.created_at) || run.head_sha !== head || typeof run.status !== 'string') throw new Error('Incomplete current-head workflow run metadata');
    const previous = latest.get(run.workflow_id);
    const delta = previous ? Date.parse(run.created_at) - Date.parse(previous.created_at) || Number(run.id) - Number(previous.id) || Number(run.run_attempt) - Number(previous.run_attempt) : 1;
    if (delta > 0) latest.set(run.workflow_id, run);
  }
  return [...latest.values()];
}

function commentDecision(comment, login, kind = 'comment', findings = []) {
  const body = comment.body || '';
  const evidence = { url: comment.html_url, commit: comment.commit_id || null, kind };
  if (significant(body, login)) return { state: 'concerns', ...evidence };
  if (login === CLAUDE) {
    const markers = outsideFences(body).split(/\r?\n/).filter(line => /^<!-- gumnut-review-verdict\//.test(line));
    const match = markers[0]?.match(/^<!-- gumnut-review-verdict\/v1 head=([0-9a-f]{40}) base=([0-9a-f]{40}) verdict=(clean|concerns|incomplete) -->$/);
    const count = (body.match(/gumnut-review-verdict\//g) || []).length;
    if (count && (count !== 1 || markers.length !== 1 || !match)) return { state: 'malformed', ...evidence };
    if (match) return { state: match[3], ...evidence, commit: match[1], base: match[2] };
  }
  if (login === CODEX) {
    // Activity metadata is not a reviewer verdict. Every other nonempty source
    // body must be understood, rather than silently preserving an old clean.
    const content = codexContent(body);
    if (codexActivity(content)) return null;
    // The explicit clean declaration is stable across observed responses; its
    // human closing sentence varies. Separate paragraphs remain unclassified.
    const clean = content?.match(/^Codex Review: Didn't find any major issues\.[^\r\n]*\r?\n+\*\*Reviewed commit:\*\*\s*`([0-9a-fA-F]{7,40})`$/);
    if (clean) return { state: 'clean', ...evidence, commit: clean[1] };
    const commits = [...body.matchAll(/\*\*Reviewed commit:\*\*\s*`([0-9a-fA-F]{7,40})`/g)];
    if (/^(?:\s*### 💡 Codex Review|Codex Review: Here are some suggestions\.)/.test(body) && commits.length === 1) {
      const wrapper = content?.match(/^(?:### 💡 Codex Review\n+Here are some automated review suggestions for this pull request\.|Codex Review: Here are some suggestions\.)\n+\*\*Reviewed commit:\*\*\s*`([0-9a-fA-F]{7,40})`$/);
      if (kind === 'review' && wrapper && comment.commit_id?.startsWith(wrapper[1]) && findings.length && findings.every(c => advisory(c.body, login))) return { state: 'clean', ...evidence, kind: 'advisory-review' };
      return { state: 'concerns', ...evidence, commit: commits[0][1] };
    }
  }
  return body.trim() ? { state: 'malformed', ...evidence } : null;
}

async function evaluate({ api, repo, number, bot }) {
  validate({ repo, number, bot });
  const root = `/repos/${repo}`;
  const pr = unwrap(await api('GET', `${root}/pulls/${number}`));
  const repository = unwrap(await api('GET', root));
  const result = { decision: 'ineligible', reason: 'closed, draft, fork, nondefault target, or self-authored PR', head: pr.head?.sha, base: pr.base?.sha, signoffs: { claude: null, codex: null } };
  if (!sha(result.head) || !sha(result.base) || !repository.default_branch) throw new Error('Invalid live PR metadata');
  if (pr.state !== 'open' || pr.draft !== false || pr.head?.repo?.full_name !== repo || pr.base?.repo?.full_name !== repo || pr.base.ref !== repository.default_branch || pr.user?.login === bot) return result;
  const [restReviews, comments, checks, statuses, reviewThreads, runs, metadata, reactions] = await Promise.all([
    pages(api, `${root}/pulls/${number}/reviews`), pages(api, `${root}/issues/${number}/comments`),
    pages(api, `${root}/commits/${result.head}/check-runs?filter=latest`, 'check_runs'),
    pages(api, `${root}/commits/${result.head}/status`, 'statuses'), threads(api, repo, number),
    pages(api, `${root}/actions/runs?head_sha=${result.head}`, 'workflow_runs'),
    reviewMetadata(api, repo, number), pages(api, `${root}/issues/${number}/reactions`),
  ]);
  const reviews = joinReviewMetadata(restReviews, metadata, bot);
  const ownDismissals = reviews.filter(r => r.user?.login === bot && r.user?.type === 'Bot' && r.state === 'DISMISSED' && r.body?.includes(MARKER));
  result.observedDismissalIds = [...new Set(ownDismissals.map(r => Number(r.id)))].sort((a, b) => a - b);
  const latestDismissalTime = ownDismissals.length ? Math.max(...ownDismissals.map(r => Date.parse(r.decision_at))) : null;
  const workflowRuns = latestWorkflowRuns(runs, result.head);
  const latest = new Map();
  for (const review of ordered(reviews)) {
    if (review.user?.login && ['APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'].includes(review.state)) latest.set(review.user.login, review);
  }
  const own = latest.get(bot);
  if (own?.user?.type === 'Bot' && own.body?.includes(MARKER)) result.existingApproval = { id: own.id, state: own.state, url: own.html_url, commit: own.commit_id, decidedAt: own.decision_at };
  let concern = null;
  let malformed = false;
  for (const [name, login] of [['claude', CLAUDE], ['codex', CODEX]]) {
    const events = comments.filter(c => c.user?.login === login && c.user?.type === 'Bot').map(c => ({ ...c, verdict: commentDecision(c, login) }));
    for (const review of reviews.filter(r => r.user?.login === login && r.user?.type === 'Bot')) {
      if (review.state === 'APPROVED') events.push({ ...review, verdict: commentDecision(review, login, 'review') || { state: 'clean', url: review.html_url, commit: review.commit_id, kind: 'review' } });
      if (review.state === 'CHANGES_REQUESTED') events.push({ ...review, verdict: { state: 'concerns', url: review.html_url, commit: review.commit_id, kind: 'review' } });
      if (review.state === 'DISMISSED') events.push({ ...review, verdict: { state: 'withdrawn', url: review.html_url, commit: review.commit_id, kind: 'review' } });
      if (review.state === 'COMMENTED') {
        const findings = reviewThreads.flatMap(t => t.comments).filter(c => c.author?.__typename === 'Bot' && c.author.login === login && positive(c.pullRequestReview?.databaseId) && Number(c.pullRequestReview.databaseId) === Number(review.id));
        events.push({ ...review, verdict: commentDecision(review, login, 'review', findings) });
      }
    }
    if (login === CODEX) {
      // Reaction user.type can be "User" even for this Bot. Match its immutable
      // account ID to authenticated Bot comments/reviews instead of trusting the
      // reaction's label or a bot-like login alone. Only PR-root thumbs-up counts.
      const actorIds = new Set([...comments, ...restReviews].filter(item => item.user?.login === CODEX && item.user.type === 'Bot' && positive(item.user.id)).map(item => Number(item.user.id)));
      for (const reaction of reactions) if (reaction.content === '+1' && reaction.user?.login === CODEX && actorIds.has(Number(reaction.user.id))) {
        if (!positive(reaction.id) || !timestamp(reaction.created_at)) throw new Error('Incomplete Codex reaction evidence');
        events.push({ ...reaction, verdict: { state: 'clean', url: `https://github.com/${repo}/pull/${number}`, commit: null, kind: 'pr-reaction', reactionId: Number(reaction.id) } });
      }
    }
    let decision, decisionTime;
    const precedence = { clean: 0, malformed: 1, withdrawn: 2, concerns: 3 };
    for (const event of ordered(events)) if (event.verdict && event.verdict.state !== 'incomplete') {
      const time = eventTime(event);
      if (!Number.isFinite(time)) throw new Error('Reviewer decision timestamp unavailable');
      // GitHub timestamps can have only second precision. IDs across comment
      // and review namespaces cannot prove which same-second decision was later.
      if (time === decisionTime && precedence[decision.state] > precedence[event.verdict.state]) continue;
      decision = event.verdict; decisionTime = time;
    }
    if (decision?.state === 'clean') result.signoffs[name] = { ...decision, decidedAt: new Date(decisionTime).toISOString() };
    if (decision?.state === 'concerns' || latest.get(login)?.state === 'CHANGES_REQUESTED') concern = `${name} requested changes: ${decision?.url || latest.get(login)?.html_url || 'review evidence'}`;
    if (decision?.state === 'malformed') malformed = true;
  }
  for (const review of latest.values()) {
    if (review.user?.type === 'User' && review.state === 'CHANGES_REQUESTED') concern = `Human changes requested by ${review.user.login}: ${review.html_url}`;
  }
  for (const thread of reviewThreads) if (!thread.isResolved) {
    const finding = thread.comments.find(c => c.author?.__typename === 'Bot' && significant(c.body, c.author?.login));
    if (finding) concern = `Significant unresolved ${finding.author.login} finding: ${finding.url || thread.id}`;
    const unknown = thread.comments.find(c => c.body?.trim() && c.author?.__typename === 'Bot' && [CLAUDE, CODEX].includes(c.author.login) && !significant(c.body, c.author.login) && !advisory(c.body, c.author.login));
    if (unknown) malformed = true;
  }
  if (concern) return { ...result, decision: 'concerns', reason: concern };
  if (malformed) return { ...result, decision: 'concerns', reason: 'Unrecognized reviewer feedback requires clarification' };
  if (!result.signoffs.claude || !result.signoffs.codex) return { ...result, decision: 'waiting', reason: 'Waiting for both standing reviewer signoffs' };
  // A safe later REQUEST_CHANGES does not erase a maintainer's dismissal. Its
  // override still requires fresh reviewer evidence after the latest dismissal.
  if (latestDismissalTime !== null && !Object.values(result.signoffs).some(s => Date.parse(s.decidedAt) > latestDismissalTime)) return { ...result, decision: 'waiting', reason: 'Coordinator review dismissed; waiting for later clean reviewer evidence' };
  if (!checks.length && !statuses.length) return { ...result, decision: 'waiting', reason: 'No current-head CI evidence' };
  if (checks.some(c => c.status !== 'completed' || !['success', 'neutral', 'skipped'].includes(c.conclusion)) || statuses.some(s => s.state !== 'success') || workflowRuns.some(r => r.status !== 'completed' || !['success', 'neutral', 'skipped'].includes(r.conclusion))) return { ...result, decision: 'waiting', reason: 'Observed current-head CI is pending or unsuccessful' };
  return { ...result, decision: 'approve', reason: 'Both standing signoffs, no significant concerns, and current-head CI passed', ci: { url: `https://github.com/${repo}/pull/${number}/checks`, checks: checks.length, statuses: statuses.length, workflows: workflowRuns.length } };
}

async function publish({ api, publisher, repo, number, bot, runURL }) {
  const result = await evaluate({ api, repo, number, bot });
  let event;
  if (result.decision === 'approve' && result.existingApproval?.state !== 'APPROVED') event = 'APPROVE';
  if (result.decision === 'concerns' && result.existingApproval?.state === 'APPROVED') event = 'REQUEST_CHANGES';
  if (!event) return { ...result, published: false };
  if (!new RegExp(`^https://github\\.com/${repo.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}/actions/runs/[1-9][0-9]*$`).test(runURL || '')) throw new Error('Invalid publication run URL');
  const body = event === 'APPROVE' ? `Approved mechanically using authenticated standing reviewer sign-offs:\n${Object.entries(result.signoffs).map(([name, s]) => `- ${name}: ${s.url} (${s.commit ? `reviewed commit ${s.commit}` : 'reviewed commit not reported'}; ${s.kind === 'pr-reaction' ? `PR-level 👍 reaction ${s.reactionId}` : s.kind}; decision ${s.decidedAt})`).join('\n')}\n\nCurrent-head CI passed for ${result.head}: ${result.ci.url}\nObserved ${result.ci.checks} checks, ${result.ci.statuses} statuses, and ${result.ci.workflows} latest workflow runs.\nNo unresolved significant or unclassified reviewer findings, or outstanding human change requests, were observed.\n\nSign-offs may predate the current commit and persist across pushes under repository policy. This internal pilot trusts existing bot feedback and repository writers; bot identity does not attest review workflow origin. The coordinator did not independently review the code.\nPublication: ${runURL}\n${MARKER}` : `Approval withheld: ${result.reason}.\n\nPublication: ${runURL}\n${MARKER}`;
  const route = `/repos/${repo}/pulls/${number}/reviews`;
  // Only the App can see its own pending reviews. A prior pending intent may
  // have been created or submitted by an interrupted run; never recover it by
  // automatically repeating a write, even if its head differs from this run.
  const visible = await pages(publisher, route);
  if (visible.some(r => r.user?.login === bot && r.user?.type === 'Bot' && r.state === 'PENDING' && r.body?.includes(MARKER))) throw new Error('Prior pending publication intent requires operator inspection');
  const submitted = ordered(visible).filter(r => r.user?.login === bot && r.user?.type === 'Bot' && ['APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'].includes(r.state)).at(-1);
  // Check every marked dismissal, including an older review beneath a newer
  // changes request. Only approval may override dismissal; withholding approval
  // through REQUEST_CHANGES remains safe when the reader snapshot lags.
  if (event === 'APPROVE') {
    const observed = new Set(result.observedDismissalIds);
    const dismissals = visible.filter(r => r.user?.login === bot && r.user?.type === 'Bot' && r.state === 'DISMISSED' && r.body?.includes(MARKER));
    if (dismissals.some(r => !positive(r.id))) throw new Error('Publisher dismissal identity unavailable');
    if (dismissals.some(r => !observed.has(Number(r.id)))) return { ...result, decision: 'waiting', reason: 'Publisher observes a dismissal absent from the read snapshot', published: false };
  }
  // A successful submit can become visible to the App before the read token.
  // Its standing disposition also prevents a later event repeating the write.
  if (submitted?.body?.includes(MARKER) && submitted.state === (event === 'APPROVE' ? 'APPROVED' : 'CHANGES_REQUESTED')) return { ...result, existingApproval: { state: submitted.state, url: submitted.html_url, commit: submitted.commit_id }, published: false };
  // Omitting event creates durable PENDING intent before the single submit.
  // Neither an uncertain create nor an uncertain submit is retried here.
  const review = unwrap(await publisher('POST', route, { commit_id: result.head, body }));
  if (!positive(review?.id) || review.user?.login !== bot || review.user?.type !== 'Bot' || review.state !== 'PENDING' || review.commit_id !== result.head || review.body !== body) throw new Error('Pending publication outcome uncertain; inspect before retry');
  await publisher('POST', `${route}/${review.id}/events`, { event, body });
  const verified = unwrap(await api('GET', `${route}/${review.id}`));
  if (verified.user?.login !== bot || verified.state !== (event === 'APPROVE' ? 'APPROVED' : 'CHANGES_REQUESTED') || verified.commit_id !== result.head || verified.body !== body) throw new Error('Publication readback failed; inspect before retry');
  return { ...result, published: true, reviewURL: verified.html_url };
}

function createApi(token) {
  if (typeof token !== 'string' || !token.trim()) throw new Error('Missing GitHub credential');
  return async (method, route, body) => {
    if (typeof route !== 'string' || /(?:^|\/)\.{1,2}(?:\/|$)/.test(route) || /%2e|%2f|%5c|\\/i.test(route)) throw new Error('Invalid GitHub API route');
    const url = new URL(route, 'https://api.github.com');
    if (url.origin !== 'https://api.github.com' || !/^\/(?:repos\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+(?:\/|$)|graphql$)/.test(url.pathname)) throw new Error('Invalid GitHub API route');
    try {
      const response = await fetch(url, { method, redirect: 'error', signal: AbortSignal.timeout(30000), headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
      if (!response.ok) throw new Error('request failed');
      return await response.json();
    } catch { throw new Error('GitHub API request failed; evidence or publication outcome unavailable'); }
  };
}
module.exports = { evaluate, publish, createApi, pages };
