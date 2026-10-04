// Reads delivery evidence only. Native gh-aw safe outputs own all publication.
const BOT = "gumnut-bot[bot]";
const EMAIL = "333470196+gumnut-bot[bot]@users.noreply.github.com";

async function verifyDelivery({
  github,
  context,
  env,
  report,
  sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  attempts = 12,
}) {
  const { owner, repo } = context.repo;
  const types = (env.OUTPUT_TYPES || "").split(",").filter(Boolean);
  const number = Number(env.PR_NUMBER);
  const hasPR = types.includes("create_pull_request");
  const hasNoop = types.includes("noop");
  const failures = [];
  for (const stage of ["AGENT", "DETECTION", "PUBLISHER"]) {
    if (env[stage] !== "success")
      failures.push(`${stage.toLowerCase()}: ${env[stage] || "unknown"}`);
  }
  if (env.OUTPUT_STATUS !== "success")
    failures.push(`safe outputs: ${env.OUTPUT_STATUS || "unknown"}`);
  if (hasPR === hasNoop) failures.push("expected exactly one of PR or noop");
  if (
    types.some(
      (type) => !["create_pull_request", "add_reviewer", "noop"].includes(type),
    )
  )
    failures.push("unexpected or incomplete safe output");

  let pr;
  let ci;
  let reviewers = [];
  // Keep published-PR evidence visible even when another stage failed.
  if (Number.isSafeInteger(number) && number > 0) {
    pr = (await github.rest.pulls.get({ owner, repo, pull_number: number }))
      .data;
    report(`PR: ${pr.html_url}\nPublished head: ${pr.head.sha}`);
    if (!hasPR || hasNoop)
      failures.push("published PR conflicts with terminal output");
    if (
      pr.user.login !== BOT ||
      pr.head.repo.full_name !== `${owner}/${repo}` ||
      !pr.head.ref.startsWith("librarian/") ||
      pr.base.ref !== context.payload.repository.default_branch
    )
      failures.push("unexpected PR identity or branch");
    const commits = await github.paginate(github.rest.pulls.listCommits, {
      owner,
      repo,
      pull_number: number,
      per_page: 100,
    });
    if (
      !commits.length ||
      commits.some(
        (commit) =>
          commit.author?.login !== BOT ||
          commit.committer?.login !== BOT ||
          commit.commit.author.name !== BOT ||
          commit.commit.author.email !== EMAIL ||
          commit.commit.committer.name !== BOT ||
          commit.commit.committer.email !== EMAIL,
      )
    )
      failures.push("published commit author/committer is not gumnut-bot[bot]");
    const requested = (
      await github.rest.pulls.listRequestedReviewers({
        owner,
        repo,
        pull_number: number,
      })
    ).data.users;
    reviewers = requested.filter(
      (user) =>
        user.type === "User" &&
        user.login !== pr.user.login &&
        !["charliecreates", "github-actions", "gumnut-bot"].includes(
          user.login,
        ),
    );
    if (!reviewers.length)
      failures.push("no eligible human review request recorded");
    report(
      `Requested human reviewers: ${reviewers.map((user) => user.login).join(", ") || "missing"}`,
    );

    // CI can take a short time to register after the App opens the PR. Wait for
    // registration, not completion; maintainers own failures and follow-up work.
    for (let attempt = 0; attempt < attempts; attempt++) {
      const runs = (
        await github.rest.actions.listWorkflowRunsForRepo({
          owner,
          repo,
          head_sha: pr.head.sha,
          event: "pull_request",
          per_page: 100,
        })
      ).data.workflow_runs;
      ci = runs.find(
        (run) =>
          run.head_sha === pr.head.sha &&
          run.path.split("@")[0] === ".github/workflows/ci.yml" &&
          (run.pull_requests.some((item) => item.number === number) ||
            run.head_branch === pr.head.ref),
      );
      if (ci) break;
      if (attempt + 1 < attempts) await sleep(10000);
    }
    if (ci) report(`CI on published head: ${ci.html_url}`);
    else failures.push("documentation CI missing on published head");
    const current = (
      await github.rest.pulls.get({ owner, repo, pull_number: number })
    ).data;
    if (current.head.sha !== pr.head.sha)
      failures.push("PR head changed during delivery verification");
  } else if (hasPR) {
    failures.push("native publisher returned no PR number");
  }
  if (hasNoop && types.includes("add_reviewer"))
    failures.push("native noop is missing or has reviewer output");
  if (failures.length)
    throw new Error(
      `Incomplete delivery: ${failures.join("; ")}. Retain any published PR for maintainer follow-up.`,
    );
  return pr ? "delivery-verified" : "noop-awaiting-native-conclusion";
}

module.exports = { verifyDelivery };
