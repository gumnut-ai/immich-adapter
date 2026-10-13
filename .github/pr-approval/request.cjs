// Read-only event routing. Contributor code and comment text never execute.
const fs = require('node:fs');
const { createApi, evaluate, publish, pages } = require('./coordinator.cjs');
const requestPath = '.github/workflows/pr-approval-request.yml';
const coordinatorPath = '.github/workflows/pr-approval-coordinator.yml';
const reviewers = new Set(['claude[bot]', 'chatgpt-codex-connector[bot]']);
const reviewEvents = new Set(['pull_request_review', 'pull_request_review_comment']);
const validNumber = value => /^[1-9][0-9]*$/.test(String(value)) && Number.isSafeInteger(Number(value));
const validBot = value => /^[A-Za-z0-9][A-Za-z0-9-]*\[bot\]$/.test(value || '');
function requireValid(value) { if (!value) throw new Error('Approval request could not be verified.'); }
function context(env, event) {
  const repo = env.GITHUB_REPOSITORY;
  requireValid(/^[A-Za-z0-9_-][A-Za-z0-9_.-]*\/[A-Za-z0-9_-][A-Za-z0-9_.-]*$/.test(repo || '') && event.repository?.full_name === repo);
  const branch = event.repository.default_branch;
  requireValid(typeof branch === 'string' && /^[A-Za-z0-9][A-Za-z0-9._/-]*$/.test(branch));
  return { repo, branch, root: `/repos/${repo}` };
}
async function eligible(api, ctx, number) {
  const pr = await api('GET', `${ctx.root}/pulls/${number}`);
  return pr.number === number && pr.state === 'open' && pr.draft === false &&
    pr.head?.repo?.full_name === ctx.repo && pr.base?.repo?.full_name === ctx.repo && pr.base.ref === ctx.branch;
}
async function writer(api, ctx, login) {
  requireValid(/^[A-Za-z0-9][A-Za-z0-9-]*$/.test(login || ''));
  const permission = await api('GET', `${ctx.root}/collaborators/${encodeURIComponent(login)}/permission`);
  return ['write', 'maintain', 'admin'].includes(permission.permission);
}
const enabled = env => ['shadow', 'publish'].includes(env.PR_APPROVAL_MODE);
async function source({ env, event, api }) {
  const ctx = context(env, event);
  if (!enabled(env)) return { eligible: false };
  if (!reviewEvents.has(env.GITHUB_EVENT_NAME)) requireValid(env.GITHUB_REF === `refs/heads/${ctx.branch}`);
  let number;
  if (env.GITHUB_EVENT_NAME === 'pull_request_target') {
    requireValid(['opened', 'synchronize', 'reopened', 'ready_for_review', 'edited'].includes(event.action));
    if (event.action === 'edited' && !event.changes?.base) return { eligible: false };
    requireValid(event.sender?.login === env.GITHUB_ACTOR);
    if (event.sender.type === 'User') {
      if (!(await writer(api, ctx, event.sender.login))) return { eligible: false };
    } else requireValid(event.sender.type === 'Bot' && validBot(event.sender.login));
    number = event.pull_request?.number;
  } else if (env.GITHUB_EVENT_NAME === 'issue_comment') {
    requireValid(['created', 'edited', 'deleted'].includes(event.action));
    if (!event.issue?.pull_request || event.comment?.user?.type !== 'Bot' || !reviewers.has(event.comment.user.login)) return { eligible: false };
    number = event.issue.number;
    requireValid(validNumber(event.comment.id));
    if (event.action !== 'deleted') {
      const comment = await api('GET', `${ctx.root}/issues/comments/${event.comment.id}`);
      requireValid(comment.user?.type === 'Bot' && comment.user.login === event.comment.user.login && comment.issue_url === `https://api.github.com${ctx.root}/issues/${number}`);
    }
  } else if (reviewEvents.has(env.GITHUB_EVENT_NAME)) {
    requireValid((env.GITHUB_EVENT_NAME === 'pull_request_review' ? ['submitted', 'edited', 'dismissed'] : ['created', 'edited', 'deleted']).includes(event.action));
    const author = env.GITHUB_EVENT_NAME === 'pull_request_review' ? event.review?.user : event.comment?.user;
    if (env.GITHUB_EVENT_NAME === 'pull_request_review_comment' && !(author?.type === 'Bot' && reviewers.has(author.login))) return { eligible: false };
    if (author?.type !== 'User' && !(author?.type === 'Bot' && reviewers.has(author.login))) return { eligible: false };
    if (event.sender?.type === 'User') {
      if (!(await writer(api, ctx, event.sender.login))) return { eligible: false };
    } else requireValid(event.sender?.type === 'Bot' && reviewers.has(event.sender.login));
    number = event.pull_request?.number;
  } else if (env.GITHUB_EVENT_NAME === 'workflow_dispatch') {
    requireValid(event.sender?.type === 'User' && event.sender.login === env.GITHUB_ACTOR);
    if (!(await writer(api, ctx, env.GITHUB_ACTOR))) return { eligible: false };
    number = event.inputs?.pr_number;
  } else return { eligible: false };
  requireValid(validNumber(number));
  number = Number(number);
  return await eligible(api, ctx, number) ? { eligible: true, number } : { eligible: false };
}
async function route({ env, event, api }) {
  const ctx = context(env, event);
  if (!enabled(env)) return { eligible: false };
  if (env.GITHUB_EVENT_NAME === 'schedule') {
    requireValid(env.GITHUB_REF === `refs/heads/${ctx.branch}`);
    const numbers = [];
    for (let page = 1; page <= 100; page++) {
      const prs = await api('GET', `${ctx.root}/pulls?state=open&per_page=100&page=${page}`);
      requireValid(Array.isArray(prs));
      for (const pr of prs) if (pr.draft === false && pr.head?.repo?.full_name === ctx.repo && pr.base?.repo?.full_name === ctx.repo && pr.base.ref === ctx.branch) {
        requireValid(validNumber(pr.number)); numbers.push(Number(pr.number));
      }
      if (prs.length < 100) return { eligible: numbers.length > 0, numbers };
    }
    throw new Error('Approval request collection exceeded its limit.');
  }
  requireValid(env.GITHUB_EVENT_NAME === 'workflow_run' && env.GITHUB_REF === `refs/heads/${ctx.branch}` && event.action === 'completed' && validNumber(event.workflow_run?.id));
  // Re-fetch the source. No artifact or contributor-supplied policy crosses jobs.
  const run = await api('GET', `${ctx.root}/actions/runs/${event.workflow_run.id}`);
  requireValid(run.id === event.workflow_run.id && run.status === 'completed' && run.repository?.full_name === ctx.repo && run.head_repository?.full_name === ctx.repo);
  const barePath = String(run.path || '').split('@')[0];
  if (barePath === coordinatorPath) return { eligible: false };
  let number;
  if (barePath === requestPath) {
    if (run.conclusion !== 'success') return { eligible: false };
    requireValid(validNumber(run.run_attempt));
    const jobs = await pages(api, `${ctx.root}/actions/runs/${run.id}/attempts/${run.run_attempt}/jobs`, 'jobs');
    // GitHub records the source's eligible output as a conditional native step.
    // A successful no-op run without that receipt was not admitted.
    if (!jobs.some(job => job.run_id === run.id && job.run_attempt === run.run_attempt &&
      job.name === 'Approval request' && job.status === 'completed' && job.conclusion === 'success' &&
      Array.isArray(job.steps) && job.steps.some(step => step.name === 'Admitted approval request' &&
        step.status === 'completed' && step.conclusion === 'success'))) return { eligible: false };
    const title = JSON.parse(run.display_title);
    requireValid(title && Object.keys(title).join(',') === 'number' && validNumber(title.number));
    number = Number(title.number);
    if (reviewEvents.has(run.event)) {
      // Review-event workflows can run at a contributor ref. Their success is
      // not approval authority: only the GitHub-associated PR requests a fresh
      // assessment of independently fetched reviewer evidence.
      requireValid(run.pull_requests?.some(pr => pr.number === number));
    } else requireValid([requestPath, `${requestPath}@${ctx.branch}`, `${requestPath}@refs/heads/${ctx.branch}`].includes(run.path) && run.head_branch === ctx.branch && ['issue_comment', 'pull_request_target', 'workflow_dispatch'].includes(run.event));
  } else {
    // CI merely requests reassessment; its code and conclusion cannot authorize
    // approval. The coordinator independently fetches all reviewer evidence.
    if (!Array.isArray(run.pull_requests) || run.pull_requests.length !== 1) return { eligible: false };
    requireValid(validNumber(run.pull_requests[0].number));
    number = Number(run.pull_requests[0].number);
  }
  return await eligible(api, ctx, number) ? { eligible: true, number } : { eligible: false };
}
function writeOutputs(file, result) {
  const values = { eligible: String(result.eligible), ...(result.number ? { number: String(result.number) } : {}), ...(typeof result.actionable === 'boolean' ? { actionable: String(result.actionable) } : {}) };
  for (const value of Object.values(values)) requireValid(/^[A-Za-z0-9_.-]+$/.test(value));
  const numbers = result.numbers || (result.number ? [result.number] : []);
  requireValid(Array.isArray(numbers) && numbers.every(validNumber));
  values.numbers = JSON.stringify(numbers.map(Number));
  fs.appendFileSync(file, Object.entries(values).map(([key, value]) => `${key}=${value}\n`).join(''));
}
async function assessMany({ api, repo, bot, numbers }) {
  requireValid(Array.isArray(numbers) && numbers.every(validNumber));
  const actionable = [], failures = [], results = [];
  for (const number of new Set(numbers.map(Number))) {
    try {
      const result = await evaluate({ api, repo, bot, number });
      if (result.decision === 'approve' && result.existingApproval?.state !== 'APPROVED' || result.decision === 'concerns' && result.existingApproval?.state === 'APPROVED') actionable.push(number);
      results.push({ number, ...result });
    } catch {
      failures.push(number);
      results.push({ number, decision: 'waiting', reason: 'Assessment unavailable; no approval action scheduled' });
    }
  }
  return { numbers: actionable, failures, results };
}
async function main(mode, env = process.env) {
  requireValid(['source', 'route', 'inspect', 'publish'].includes(mode));
  const api = createApi(env.GH_TOKEN);
  if (['source', 'route'].includes(mode)) {
    const event = JSON.parse(fs.readFileSync(env.GITHUB_EVENT_PATH, 'utf8'));
    const result = await { source, route }[mode]({ env, event, api });
    writeOutputs(env.GITHUB_OUTPUT, result);
    console.log(JSON.stringify(result));
  } else {
    requireValid(enabled(env) && validBot(env.PR_APPROVAL_BOT_LOGIN));
    const args = { api, repo: env.GITHUB_REPOSITORY, bot: env.PR_APPROVAL_BOT_LOGIN };
    if (mode === 'inspect') {
      const targets = JSON.parse(env.PR_NUMBERS);
      const assessment = await assessMany({ ...args, numbers: targets });
      for (const result of assessment.results) console.log(JSON.stringify(result));
      for (const number of assessment.failures) console.error(`::warning::Assessment unavailable for PR #${number}; abstained.`);
      if (env.GITHUB_STEP_SUMMARY && assessment.failures.length) {
        fs.appendFileSync(env.GITHUB_STEP_SUMMARY, `Approval assessment incomplete for PRs ${assessment.failures.map(n => `#${n}`).join(', ')}. No action was scheduled for those PRs; inspect this run before treating their review as complete.\n`);
      }
      writeOutputs(env.GITHUB_OUTPUT, { eligible: true, actionable: assessment.numbers.length > 0, numbers: assessment.numbers });
    }
    else {
      requireValid(env.PR_APPROVAL_MODE === 'publish' && validNumber(env.GITHUB_RUN_ID) && validNumber(env.PR_NUMBER));
      console.log(JSON.stringify(await publish({ ...args, number: Number(env.PR_NUMBER), publisher: createApi(env.PUBLISH_TOKEN), runURL: `https://github.com/${args.repo}/actions/runs/${env.GITHUB_RUN_ID}` })));
    }
  }
}
if (require.main === module) main(process.argv[2]).catch(() => { console.error('::error::Approval request could not be verified.'); process.exitCode = 1; });
module.exports = { source, route, writeOutputs, assessMany, main };
