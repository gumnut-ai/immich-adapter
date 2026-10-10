const fs = require('node:fs');

const workflowPath = '.github/workflows/claude-review.yml';
const deniedMessage = 'Claude review routing could not be verified.';

function requireValid(condition) {
  if (!condition) throw new Error(deniedMessage);
}

function positiveInteger(value) {
  requireValid(/^[1-9][0-9]*$/.test(String(value)) && Number.isSafeInteger(Number(value)));
  return Number(value);
}

function sha(value) {
  requireValid(typeof value === 'string' && /^[0-9a-f]{40}$/i.test(value));
  return value.toLowerCase();
}

function context(env, event) {
  requireValid(typeof env.GITHUB_REPOSITORY === 'string' && /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(env.GITHUB_REPOSITORY));
  const [owner, repo] = env.GITHUB_REPOSITORY.split('/');
  requireValid(!['.', '..'].includes(owner) && !['.', '..'].includes(repo));
  const branch = event.repository?.default_branch;
  requireValid(event.repository?.full_name === env.GITHUB_REPOSITORY && typeof branch === 'string' && /^[A-Za-z0-9][A-Za-z0-9._/-]*$/.test(branch));
  return { owner, repo, branch, fullName: env.GITHUB_REPOSITORY, root: `/repos/${owner}/${repo}` };
}

function createApi(env, fetchImpl = fetch) {
  requireValid(typeof env.GH_TOKEN === 'string' && env.GH_TOKEN.length > 0);
  return async (method, route, body) => {
    try {
      const response = await fetchImpl(`https://api.github.com${route}`, {
        method,
        headers: {
          Authorization: `Bearer ${env.GH_TOKEN}`,
          Accept: 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28',
          'Content-Type': 'application/json',
        },
        signal: AbortSignal.timeout(30_000),
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      requireValid(response.ok);
      return response.status === 204 ? undefined : await response.json();
    } catch {
      // A failed dispatch may have been accepted. Never retry a POST here.
      throw new Error(deniedMessage);
    }
  };
}

async function writer(api, ctx, login) {
  requireValid(typeof login === 'string' && /^[A-Za-z0-9][A-Za-z0-9-]*$/.test(login));
  const result = await api('GET', `${ctx.root}/collaborators/${encodeURIComponent(login)}/permission`);
  return ['write', 'maintain', 'admin'].includes(result?.permission);
}

function eligiblePr(pr, ctx, number) {
  return pr?.number === number && pr.state === 'open' && pr.draft === false &&
    pr.head?.repo?.full_name === ctx.fullName && pr.base?.repo?.full_name === ctx.fullName &&
    pr.base?.ref === ctx.branch;
}

async function dispatch({ env, event, api }) {
  const ctx = context(env, event);
  requireValid(env.GITHUB_EVENT_NAME === 'pull_request_target');
  if (env.CLAUDE_REVIEW_ENABLED !== 'true') return { eligible: false };
  const number = positiveInteger(event.pull_request?.number);
  const runId = positiveInteger(env.GITHUB_RUN_ID);
  const head = sha(event.pull_request?.head?.sha);
  if (!eligiblePr(event.pull_request, ctx, number) || event.sender?.type !== 'User') return { eligible: false };
  if (!(await writer(api, ctx, event.sender.login))) return { eligible: false };
  await api('POST', `${ctx.root}/actions/workflows/claude-review.yml/dispatches`, {
    ref: ctx.branch,
    inputs: { pr_number: String(number), expected_head: head, origin_run_id: String(runId) },
  });
  return { eligible: true };
}

async function prepare({ env, event, api, output }) {
  const ctx = context(env, event);
  requireValid(env.GITHUB_EVENT_NAME === 'workflow_dispatch' && env.GITHUB_REF === `refs/heads/${ctx.branch}`);
  const number = positiveInteger(event.inputs?.pr_number);
  const expectedHead = event.inputs?.expected_head ? sha(event.inputs.expected_head) : undefined;
  const originId = event.inputs?.origin_run_id ? positiveInteger(event.inputs.origin_run_id) : undefined;
  const skip = () => { output({ eligible: 'false' }); return { eligible: false }; };
  if (env.CLAUDE_REVIEW_ENABLED !== 'true') return skip();
  const pr = await api('GET', `${ctx.root}/pulls/${number}`);
  if (!eligiblePr(pr, ctx, number)) return skip();
  const head = sha(pr.head.sha);
  if (expectedHead && expectedHead !== head) return skip();
  if (env.GITHUB_ACTOR === 'github-actions[bot]' && event.sender?.login === 'github-actions[bot]' && event.sender?.type === 'Bot') {
    requireValid(originId && expectedHead);
    const origin = await api('GET', `${ctx.root}/actions/runs/${originId}`);
    requireValid(origin?.repository?.full_name === ctx.fullName && origin.event === 'pull_request_target' &&
      [workflowPath, `${workflowPath}@${ctx.branch}`, `${workflowPath}@refs/heads/${ctx.branch}`].includes(origin.path) && origin.actor?.type === 'User' &&
      Array.isArray(origin.pull_requests) && origin.pull_requests.some(item => item.number === number && item.head?.sha === head));
    if (!(await writer(api, ctx, origin.actor.login))) return skip();
  } else {
    requireValid(event.sender?.type === 'User' && event.sender.login === env.GITHUB_ACTOR && !originId);
    if (!(await writer(api, ctx, env.GITHUB_ACTOR))) return skip();
  }
  const runId = positiveInteger(env.GITHUB_RUN_ID);
  const check = await api('POST', `${ctx.root}/check-runs`, {
    name: 'Claude PR review', head_sha: head, status: 'in_progress',
    details_url: `https://github.com/${ctx.fullName}/actions/runs/${runId}`,
  });
  const checkId = positiveInteger(check?.id);
  output({ eligible: 'true', pr_number: String(number), head_sha: head, check_id: String(checkId), owner: ctx.owner, repo: ctx.repo });
  return { eligible: true };
}

async function finish({ env, event, api }) {
  const ctx = context(env, event);
  requireValid(env.GITHUB_EVENT_NAME === 'workflow_dispatch' && env.GITHUB_REF === `refs/heads/${ctx.branch}`);
  const checkId = positiveInteger(env.CHECK_ID);
  const conclusion = env.REVIEW_JOB_STATUS === 'cancelled' ? 'cancelled' :
    env.REVIEW_JOB_STATUS === 'success' && env.CLAUDE_CONCLUSION === 'success' ? 'success' : 'failure';
  await api('PATCH', `${ctx.root}/check-runs/${checkId}`, { status: 'completed', conclusion });
}

function writeOutput(file, values) {
  for (const [key, value] of Object.entries(values)) {
    requireValid(/^[a-z_]+$/.test(key) && typeof value === 'string' && /^[A-Za-z0-9_.-]+$/.test(value));
  }
  fs.appendFileSync(file, Object.entries(values).map(([key, value]) => `${key}=${value}\n`).join(''));
}

async function main(mode, env = process.env) {
  requireValid(['dispatch', 'prepare', 'finish'].includes(mode));
  const event = JSON.parse(fs.readFileSync(env.GITHUB_EVENT_PATH, 'utf8'));
  const api = createApi(env);
  await { dispatch, prepare, finish }[mode]({ env, event, api, output: values => writeOutput(env.GITHUB_OUTPUT, values) });
}

if (require.main === module) {
  main(process.argv[2]).catch(() => {
    console.error(`::error::${deniedMessage}`);
    process.exitCode = 1;
  });
}

module.exports = { dispatch, prepare, finish, createApi, writeOutput, main };
