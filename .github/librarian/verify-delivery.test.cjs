const { test } = require("node:test");
const assert = require("node:assert/strict");
// Exercise both implementations with the same evidence and failure cases.
const daemon = process.env.TEST_DELIVERY_DAEMON || "librarian";
const { verifyDelivery } = require(`../${daemon}/verify-delivery.cjs`);

function fixture() {
  const identity = {
    name: "gumnut-bot[bot]",
    email: "333470196+gumnut-bot[bot]@users.noreply.github.com",
  };
  const pr = {
    user: { login: "gumnut-bot[bot]" },
    html_url: "https://github.com/owner/repo/pull/7",
    head: {
      sha: "final-head",
      ref: `${daemon}/docs`,
      repo: { full_name: "owner/repo" },
    },
    base: { ref: "main" },
  };
  const commits = [
    {
      author: pr.user,
      committer: pr.user,
      commit: { author: identity, committer: identity },
    },
  ];
  const users = [{ type: "User", login: "maintainer" }];
  const runs = [
    {
      head_sha: "final-head",
      path: ".github/workflows/ci.yml@main",
      pull_requests: [{ number: 7 }],
      html_url: "https://github.com/owner/repo/actions/runs/8",
    },
  ];
  const reports = [];
  const github = {
    rest: {
      pulls: {
        get: async () => ({ data: pr }),
        listCommits: async () => ({ data: commits }),
        listRequestedReviewers: async () => ({ data: { users } }),
      },
      actions: {
        listWorkflowRunsForRepo: async () => ({
          data: { workflow_runs: runs },
        }),
      },
    },
    paginate: async (method, args) => (await method(args)).data,
  };
  return {
    pr,
    commits,
    users,
    runs,
    reports,
    args: {
      github,
      context: {
        repo: { owner: "owner", repo: "repo" },
        payload: { repository: { default_branch: "main" } },
      },
      env: {
        AGENT: "success",
        DETECTION: "success",
        PUBLISHER: "success",
        OUTPUT_STATUS: "success",
        OUTPUT_TYPES: "create_pull_request,add_reviewer",
        PR_NUMBER: "7",
      },
      report: (text) => reports.push(text),
      attempts: 1,
    },
  };
}

test("reads back App identity, both commit identities, human request, and CI on final head", async () => {
  const f = fixture();
  assert.equal(await verifyDelivery(f.args), "delivery-verified");
  assert.match(f.reports.join("\n"), /actions\/runs\/8/);
});

test("missing CI or reviewer keeps the PR visible and reports incomplete", async () => {
  for (const missing of ["runs", "users"]) {
    const f = fixture();
    f[missing].length = 0;
    await assert.rejects(verifyDelivery(f.args), /Incomplete delivery/);
    assert.match(f.reports.join("\n"), /pull\/7/);
  }
});

test("PR plus noop and failed publication cannot become completed delivery", async () => {
  for (const override of [
    { OUTPUT_TYPES: "create_pull_request,noop" },
    { PUBLISHER: "failure" },
    { DETECTION: "failure" },
  ]) {
    const f = fixture();
    Object.assign(f.args.env, override);
    await assert.rejects(verifyDelivery(f.args), /Incomplete delivery/);
  }
});

test("rejects human commit attribution, bot reviewer, and CI for a previous head", async () => {
  for (const mutate of [
    (f) => {
      f.commits[0].commit.committer = {
        name: "Human",
        email: "human@example.com",
      };
    },
    (f) => {
      f.users[0].type = "Bot";
    },
    (f) => {
      f.runs[0].head_sha = "previous-head";
    },
  ]) {
    const f = fixture();
    mutate(f);
    await assert.rejects(verifyDelivery(f.args), /Incomplete delivery/);
  }
});

test("rejects a head change while evidence is being gathered", async () => {
  const f = fixture();
  let reads = 0;
  f.args.github.rest.pulls.get = async () => ({
    data:
      ++reads === 1
        ? f.pr
        : { ...f.pr, head: { ...f.pr.head, sha: "new-head" } },
  });
  await assert.rejects(verifyDelivery(f.args), /head changed/);
});

test("noop stays pending native conclusion and missing outputs fail", async () => {
  const f = fixture();
  f.args.env.PR_NUMBER = "";
  f.args.env.OUTPUT_TYPES = "noop";
  assert.equal(await verifyDelivery(f.args), "noop-awaiting-native-conclusion");
  f.args.env.OUTPUT_TYPES = "";
  await assert.rejects(verifyDelivery(f.args), /exactly one/);
});


test("automation logins cannot satisfy human handoff even when typed User", async () => {
  for (const login of ["CharlieHelps", "CHARLIECREATES", "GitHub-Actions", "GUMNUT-BOT",
    "ChatGPT-Codex-Connector", "COPILOT", "automation[BoT]"]) {
    const f = fixture();
    f.users[0].login = login;
    await assert.rejects(verifyDelivery(f.args), /no eligible human review request/);
    assert.match(f.reports.join("\n"), /Requested human reviewers: missing/);
  }
});


test("each daemon rejects a PR from the other daemon's branch", async () => {
  const f = fixture();
  f.pr.head.ref = `${daemon === "librarian" ? "maintainer" : "librarian"}/proposal`;
  await assert.rejects(verifyDelivery(f.args), /unexpected PR identity or branch/);
  assert.match(f.reports.join("\n"), /pull\/7/);
});
