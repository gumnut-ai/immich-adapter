// Trusted metadata checks only. Never load code from a contributor revision.
const BOT = process.env.PR_REVIEW_BOT_LOGIN;
const validBot = value => /^[a-zA-Z0-9-]+\[bot\]$/.test(value || '');
const protocol = 'pr-review-native/v2';
const digest = body => require('node:crypto').createHash('sha256').update(body.trim()).digest('hex');
const inlineMarker = entries => `<!-- pr-review-inline/v1 ${Buffer.from(JSON.stringify(entries)).toString('base64')} -->`;
const coverageDeclaration = (head, base, coverage) => `PR review coverage: head=${head} base=${base} ${coverage}`;
const marker = (head, base, coverage) => `<!-- ${protocol} head=${head} base=${base} coverage=${coverage} -->`;
const validNumber = value => /^[1-9][0-9]*$/.test(String(value)) && Number.isSafeInteger(Number(value));
const validSHA = value => /^[0-9a-f]{40}$/.test(value || '');
const inlineBlocking = body => /^\*\*[^*\r\n]+\*\* \| `🔴 blocking` \| `§ [^`\r\n]+`(?:\r?\n|$)/.test(body || '');
// Fenced examples are evidence text, not standalone finding declarations.
// CommonMark fences allow up to three spaces, matching delimiters, and a
// closing run at least as long as the opener. An unclosed fence ends at EOF.
function outsideFences(body) {
  const prose = [];
  let fence;
  for (const line of (body || '').split(/\r?\n/)) {
    const delimiter = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (fence) {
      if (delimiter && delimiter[1][0] === fence[0] && delimiter[1].length >= fence.length && /^[ \t]*$/.test(delimiter[2])) fence = undefined;
    } else if (delimiter && (delimiter[1][0] === '~' || !delimiter[2].includes('`'))) {
      fence = delimiter[1];
    } else {
      prose.push(line);
    }
  }
  return prose.join('\n');
}
const bodyBlocking = body => /^(?:\*\*)?🔴 blocking:(?:\*\*)?[ \t]+\S.*$/m.test(outsideFences(body));
const ambiguousInline = body => (body || '').split(/\r?\n/)[0].includes('🔴 blocking') && !inlineBlocking(body);
const ambiguousBody = body => /^(?:\*\*)?🔴 blocking\b/m.test(outsideFences(body)) && !bodyBlocking(body);

function request(context) {
  const p = context.payload;
  if (context.eventName === 'pull_request_target') {
    if (!['opened', 'reopened', 'ready_for_review', 'synchronize', 'review_requested', 'edited'].includes(p.action)) return null;
    if (p.action === 'edited' && !p.changes?.base) return null;
    if (p.action === 'review_requested' && !['CharlieHelps', BOT].includes(p.requested_reviewer?.login)) return null;
    return { number: p.pull_request.number, manual: p.action === 'review_requested' };
  }
  if (context.eventName === 'issue_comment') {
    if (p.action !== 'created' || !p.issue?.pull_request || p.comment?.user?.type !== 'User' || !['OWNER', 'MEMBER', 'COLLABORATOR'].includes(p.comment.author_association)) return null;
    // Complete command lines only; quoted mentions or prose do not authorize work.
    if (!/^(?:\/review|@(?:CharlieHelps|gumnut-reviewer) review)(?:[ \t]+[^\r\n]*)?$/i.test(p.comment.body.trim())) return null;
    return { number: p.issue.number, manual: true };
  }
  if (context.eventName === 'workflow_dispatch') {
    if (context.ref !== `refs/heads/${p.repository.default_branch}` || p.inputs?.aw_context) throw new Error('Dispatch requires default branch and no prompt context');
    if (!validNumber(p.inputs?.pull_request_number)) throw new Error('Expected a positive pull request number');
    return { number: Number(p.inputs.pull_request_number), manual: true };
  }
  return null;
}

async function admitComment({ github, context }) {
  if (context.eventName !== 'issue_comment') return false;
  const target = request(context);
  if (!target || context.actor !== context.payload.comment.user.login) return false;
  const { data } = await github.rest.repos.getCollaboratorPermissionLevel({ ...context.repo, username: context.actor });
  return ['admin', 'maintain', 'write'].includes(data.permission);
}

async function commentRunRequest({ github, context }) {
  const event = context.payload.workflow_run;
  if (context.payload.action !== 'completed' || !validNumber(event?.id)) return null;
  const { data: run } = await github.rest.actions.getWorkflowRun({ ...context.repo, run_id: Number(event.id) });
  const repo = `${context.repo.owner}/${context.repo.repo}`;
  if (Number(run.id) !== Number(event.id) || event.display_title !== run.display_title || context.ref !== `refs/heads/${context.payload.repository.default_branch}` || run.event !== 'issue_comment' || run.status !== 'completed' || run.conclusion !== 'success' || run.head_branch !== context.payload.repository.default_branch || run.repository?.full_name !== repo || run.head_repository?.full_name !== repo || !validSHA(run.head_sha) || !validNumber(run.workflow_id)) return null;
  const { data: workflow } = await github.rest.actions.getWorkflow({ ...context.repo, workflow_id: run.workflow_id });
  if (workflow.path !== '.github/workflows/pr-review-comment.yml') return null;
  // Numeric IDs and a coarse candidate Boolean only: no artifact, code, prompt, or comment text crosses jobs.
  let ids;
  try { ids = JSON.parse(run.display_title); } catch { return null; }
  if (!ids || Object.keys(ids).sort().join(',') !== 'candidate,comment,number' || ids.candidate !== true || typeof ids.number !== 'number' || typeof ids.comment !== 'number' || !validNumber(ids.number) || !validNumber(ids.comment)) return null;
  const { data: comment } = await github.rest.issues.getComment({ ...context.repo, comment_id: Number(ids.comment) });
  const api = context.apiUrl || process.env.GITHUB_API_URL || 'https://api.github.com';
  if (comment.issue_url !== `${api}/repos/${repo}/issues/${ids.number}` || comment.user?.login !== run.actor?.login) return null;
  const { data: issue } = await github.rest.issues.get({ ...context.repo, issue_number: Number(ids.number) });
  if (!issue.pull_request) return null;
  const original = { ...context, eventName: 'issue_comment', actor: comment.user.login, payload: { ...context.payload, action: 'created', issue, comment } };
  const target = request(original);
  return target ? { ...target, actor: comment.user.login } : null;
}

async function reviews(github, repo, number) {
  return github.paginate(github.rest.pulls.listReviews, { ...repo, pull_number: number, per_page: 100 });
}
function covered(items, head, base) {
  return validSHA(head) && validSHA(base) && items.some(r => r.user?.login === BOT && r.commit_id === head && ['APPROVED', 'COMMENTED'].includes(r.state) && r.body?.includes(marker(head, base, 'complete')) && inlineManifest(r.body) !== null);
}

function inlineManifest(body) {
  const matches = [...(body || '').matchAll(/<!-- pr-review-inline\/v1 ([A-Za-z0-9+/=]+) -->/g)];
  if (matches.length !== 1 || (body.match(/<!-- pr-review-inline\/v1/g) || []).length !== 1) return null;
  try {
    const encoded = matches[0][1];
    const decoded = Buffer.from(encoded, 'base64');
    if (decoded.toString('base64') !== encoded) return null;
    const entries = JSON.parse(decoded.toString('utf8'));
    if (!Array.isArray(entries) || entries.length > 100 || entries.some(e => !e || typeof e !== 'object' || Object.keys(e).sort().join(',') !== 'digest,line,path,side,start' || typeof e.path !== 'string' || !e.path || typeof e.line !== 'number' || !validNumber(e.line) || !['LEFT', 'RIGHT'].includes(e.side) || !/^[0-9a-f]{64}$/.test(e.digest) || (e.start !== null && (typeof e.start !== 'number' || !validNumber(e.start) || e.start > e.line)))) return null;
    return entries;
  } catch { return null; }
}
async function findingsDelivered(github, repo, number, review) {
  const expected = inlineManifest(review.body);
  if (expected === null) return false;
  const comments = await github.paginate(github.rest.pulls.listCommentsForReview, { ...repo, pull_number: Number(number), review_id: review.id, per_page: 100 });
  if (comments.length !== expected.length) return false;
  if (ambiguousBody(review.body) || comments.some(item => ambiguousInline(item.body))) return false;
  const blocking = bodyBlocking(review.body) || comments.some(item => inlineBlocking(item.body));
  if (review.state !== (blocking ? 'COMMENTED' : 'APPROVED')) return false;
  const remaining = [...comments];
  return expected.every(entry => {
    const index = remaining.findIndex(c => c.user?.login === BOT && c.path === entry.path && (c.original_line ?? c.line) === Number(entry.line) && c.side === entry.side && (c.original_start_line ?? c.start_line ?? null) === (entry.start ?? null) && typeof c.body === 'string' && digest(c.body) === entry.digest);
    if (index < 0) return false;
    remaining.splice(index, 1);
    return true;
  });
}
async function deliveredCoverage(github, repo, number, items, head, base) {
  for (const review of items) {
    if (covered([review], head, base) && await findingsDelivered(github, repo, number, review)) return true;
  }
  return false;
}

async function preparePublication({ github, context, output, binding, sanitize }) {
  const coverage = validateOutput(output, binding);
  const findings = output.items.filter(item => item.type === 'create_pull_request_review_comment');
  if (findings.length > 100) throw new Error('Inline output exceeds native publication limit');
  const files = await github.paginate(github.rest.pulls.listFiles, { ...context.repo, pull_number: Number(binding.number), per_page: 100 });
  const paths = new Set(files.flatMap(file => [file.filename, file.previous_filename].filter(Boolean)));
  const entries = findings.map(item => {
    if (!paths.has(item.path)) throw new Error(`Finding path outside current PR diff: ${item.path}`);
    if (!validNumber(item.line) || (item.start_line != null && (!validNumber(item.start_line) || Number(item.start_line) > Number(item.line))) || !['LEFT', 'RIGHT'].includes(item.side || 'RIGHT') || typeof item.body !== 'string') throw new Error('Invalid inline finding location or body');
    return { path: item.path, line: Number(item.line), side: item.side || 'RIGHT', start: item.start_line == null ? null : Number(item.start_line), digest: digest(sanitize(item.body.trim())) };
  });
  const submit = output.items.find(item => item.type === 'submit_pull_request_review');
  // Only trusted code creates this delivery manifest; replace any agent-supplied copy.
  submit.body = submit.body.replace(/<!-- pr-review-(?:inline|native)\/v[12][\s\S]*?-->/g, '').trimEnd() + '\n\n' + marker(binding.head, binding.base, coverage) + '\n' + inlineMarker(entries);
  await assertFresh({ github, context, ...binding });
  return output;
}

async function admit({ github, context, enabled }) {
  if (enabled !== 'true') return { eligible: 'false', reason: 'disabled' };
  if (!validBot(BOT)) throw new Error('Configure exact reviewer App bot login');
  const target = context.eventName === 'workflow_run' ? await commentRunRequest({ github, context }) : context.eventName === 'issue_comment' ? null : request(context);
  if (!target) return { eligible: 'false', reason: 'unmatched-event' };
  if (!validNumber(target.number)) throw new Error('Invalid pull request number');
  if (target.manual) {
    const { data } = await github.rest.repos.getCollaboratorPermissionLevel({ ...context.repo, username: target.actor || context.actor });
    if (!['admin', 'maintain', 'write'].includes(data.permission)) return { eligible: 'false', reason: 'unauthorized-request' };
  }
  const { data: pr } = await github.rest.pulls.get({ ...context.repo, pull_number: target.number });
  if (pr.state !== 'open' || pr.draft) return { eligible: 'false', reason: 'closed-or-draft' };
  if (!validSHA(pr.head.sha) || !validSHA(pr.base.sha)) throw new Error('Invalid GitHub revision');
  if (pr.user.login !== BOT && await deliveredCoverage(github, context.repo, target.number, await reviews(github, context.repo, target.number), pr.head.sha, pr.base.sha)) return { eligible: 'false', reason: 'already-reviewed-current-diff' };
  return { eligible: 'true', reason: 'review-required', number: String(target.number), head: pr.head.sha, base: pr.base.sha, author: pr.user.login, bot: BOT };
}

async function assertFresh({ github, context, number, head, base }) {
  if (!validNumber(number) || !validSHA(head) || !validSHA(base)) throw new Error('Invalid review binding');
  const { data: pr } = await github.rest.pulls.get({ ...context.repo, pull_number: Number(number) });
  if (pr.state !== 'open' || pr.draft || pr.head.sha !== head || pr.base.sha !== base) throw new Error('Review incomplete: pull request closed, became draft, or head/base changed');
  if (pr.user.login !== BOT && await deliveredCoverage(github, context.repo, number, await reviews(github, context.repo, Number(number)), head, base)) throw new Error('Review already published for this head/base');
  return pr;
}

function validateOutput(output, { number, head, base, author }) {
  if (!validNumber(number) || !validSHA(head) || !validSHA(base)) throw new Error('Invalid review binding');
  if (!Array.isArray(output.errors) || output.errors.length) throw new Error('Review incomplete: native collector rejected output declarations');
  if (!Array.isArray(output.items)) throw new Error('Missing native safe-output declarations');
  const submits = output.items.filter(item => item.type === 'submit_pull_request_review');
  if (submits.length !== 1) throw new Error('Exactly one formal review declaration is required');
  const submit = submits[0];
  for (const item of output.items) {
    if (!['submit_pull_request_review', 'create_pull_request_review_comment'].includes(item.type)) throw new Error('Unexpected review output type');
    if (item.repo || (item.pull_request_number != null && String(item.pull_request_number) !== String(number))) throw new Error('Review target differs from admission');
  }
  if (!['APPROVE', 'COMMENT'].includes(submit.event)) throw new Error('Unsupported formal review event');
  const declarations = typeof submit.body === 'string' ? submit.body.split(/\r?\n/).filter(line => line.startsWith('PR review coverage:')) : [];
  const complete = declarations[0] === coverageDeclaration(head, base, 'complete');
  const incomplete = declarations[0] === coverageDeclaration(head, base, 'incomplete');
  if (declarations.length !== 1 || complete === incomplete) throw new Error('Exactly one current head/base coverage declaration is required');
  if (ambiguousBody(submit.body) || output.items.filter(item => item.type === 'create_pull_request_review_comment').some(item => ambiguousInline(item.body))) throw new Error('Ambiguous blocking finding declaration');
  const blocking = bodyBlocking(submit.body) || output.items.filter(item => item.type === 'create_pull_request_review_comment').some(item => inlineBlocking(item.body));
  if (author === BOT && complete) throw new Error('Own-author review requires incomplete COMMENT coverage');
  const expected = complete && !blocking ? 'APPROVE' : 'COMMENT';
  if (submit.event !== expected) throw new Error('Formal review event differs from complete/clean or blocking/incomplete policy');
  return complete ? 'complete' : 'incomplete';
}

async function verifyDelivery({ github, context, number, head, base, agent, detection, publisher }) {
  if ([agent, detection, publisher].some(result => result !== 'success')) throw new Error('Review incomplete: inference, detection or publication did not succeed');
  const { data: pr } = await github.rest.pulls.get({ ...context.repo, pull_number: Number(number) });
  if (!validNumber(number) || !validSHA(head) || !validSHA(base) || pr.state !== 'open' || pr.draft || pr.head.sha !== head || pr.base.sha !== base) throw new Error('Review incomplete: current open head/base requires a new review');
  const runURL = `${context.serverUrl || 'https://github.com'}/${context.repo.owner}/${context.repo.repo}/actions/runs/${context.runId}`;
  const delivered = (await reviews(github, context.repo, Number(number))).filter(r => r.user?.login === BOT && r.commit_id === head && r.body?.includes(runURL));
  if (delivered.length !== 1 || !['APPROVED', 'COMMENTED'].includes(delivered[0].state)) throw new Error('Current run formal review not confirmed');
  if (pr.user.login === BOT) throw new Error('Own-author review cannot establish complete approval coverage');
  if (!delivered[0].body.includes(marker(head, base, 'complete'))) throw new Error('Formal limitation or different-base review delivered; coverage remains incomplete');
  if (!await findingsDelivered(github, context.repo, number, delivered[0])) throw new Error('Formal review delivered but declared inline findings are missing or altered');
  return delivered[0].html_url;
}
module.exports = { request, admitComment, commentRunRequest, marker, coverageDeclaration, covered, admit, assertFresh, validateOutput, preparePublication, inlineMarker, verifyDelivery };
