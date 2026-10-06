const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const { wrapReviewer } = require("./reviewer-handler.cjs");

const setup = process.env.GH_AW_SETUP_DIR;
if (!setup) throw Error("GH_AW_SETUP_DIR must identify pinned v0.89.21 setup/js");
const { main } = require(path.join(setup, "add_reviewer.cjs"));


function proposalOutput() {
  return { errors: [], items: [
    { type: "create_pull_request", temporary_id: "aw_proposal" },
    { type: "add_reviewer", pull_request_number: "aw_proposal", reviewers: ["ternarybits"] },
  ] };
}

function fixture() {
  const calls = [];
  global.context = { eventName: "schedule", repo: { owner: "gumnut-ai", repo: "immich-adapter" }, payload: {} };
  global.core = { info() {}, warning() {}, debug() {}, error() {} };
  global.github = { rest: {
    issues: { get: async () => ({ data: { labels: [{ name: "librarian" }] } }) },
    pulls: {
      get: async () => ({ data: { requested_reviewers: [], requested_teams: [] } }),
      listReviews: async () => ({ data: [] }),
      requestReviewers: async (args) => { calls.push(args); return { data: { requested_reviewers: [{ login: "ternarybits" }] } }; },
    },
  } };
  return { calls, message: { reviewers: ["ternarybits"], pull_request_number: "aw_proposal" },
    map: { aw_proposal: { repo: "gumnut-ai/immich-adapter", number: 2524 } } };
}

test("native reviewer API receives the actual published number for bare and hash IDs", async () => {
  for (const id of ["aw_proposal", "#aw_proposal", "aw_Proposal", "#AW_Proposal"]) {
    const f = fixture();
    const handler = await wrapReviewer(main)({ max: 1, target: "*", required_labels: ["librarian"] });
    const result = await handler({ ...f.message, pull_request_number: id }, f.map);
    assert.equal(result.success, true, result.error);
    assert.equal(f.calls[0].pull_number, 2524);
    assert.deepEqual(f.calls[0].reviewers, ["ternarybits"]);
  }
});

test("installed adapter uses native dependency ordering before resolving reviewer IDs", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "librarian-handler-"));
  try {
    fs.cpSync(setup, dir, { recursive: true });
    require("./prepare-reviewer-handler.cjs").prepare(dir, proposalOutput());
    const { sortMessageIndicesByTemporaryIdDependencies: sort } = require(path.join(dir, "safe_output_handler_manager.cjs"));
    const f = fixture();
    const messages = [{ type: "add_reviewer", ...f.message, pull_request_number: "#AW_Proposal" },
      { type: "create_pull_request", temporary_id: "aw_proposal" }];
    assert.deepEqual(sort(messages), [1, 0]);
    const installed = require(path.join(dir, "add_reviewer.cjs"));
    const handler = await installed.main({ max: 1, target: "*" });
    assert.equal((await handler(messages[0], f.map)).success, true);
    assert.equal(f.calls[0].pull_number, 2524);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("invalid, old, cross-repository and automation requests never call the reviewer API", async () => {
  for (const mutate of [
    f => { f.message.pull_request_number = 2524; },
    f => { f.map = {}; },
    f => { f.map.aw_proposal.repo = "other/repo"; },
    f => { f.message.repo = "other/repo"; },
    f => { f.message.reviewers = ["CharlieHelps"]; },
    f => { f.message.reviewers = ["CHARLIECREATES"]; },
    f => { f.message.reviewers = ["gumnut-bot[bot]"]; },
    f => { f.map.aw_proposal.number = 0; },
    f => { f.map.aw_proposal.number = "2524"; },
    f => { f.message.reviewers = []; },
    f => { f.message.reviewers = ["human", "other"]; },
    f => { f.message.reviewers = ["COPILOT"]; },
    f => { f.message.reviewers = ["chatgpt-codex-connector"]; },
    f => { f.message.team_reviewers = ["team"]; },
  ]) {
    const f = fixture(); mutate(f);
    const handler = await wrapReviewer(main)({ max: 1, target: "*" });
    assert.equal((await handler(f.message, f.map)).success, false);
    assert.equal(f.calls.length, 0);
  }
});

test("native review API failure remains a failed output", async () => {
  const f = fixture();
  global.github.rest.pulls.requestReviewers = async () => { throw Error("fixture denied"); };
  const handler = await wrapReviewer(main)({ max: 1, target: "*" });
  assert.equal((await handler(f.message, f.map)).success, false);
});


test("complete helper drift fails before modifying either native helper", () => {
  for (const file of ["add_reviewer.cjs", "safe_output_handler_manager.cjs"]) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "reviewer-drift-"));
    try {
      fs.cpSync(setup, dir, { recursive: true });
      fs.appendFileSync(path.join(dir, file), "\n// fixture drift\n");
      const names = ["add_reviewer.cjs", "safe_output_handler_manager.cjs"];
      const before = names.map(name => fs.readFileSync(path.join(dir, name), "utf8"));
      assert.throws(() => require("./prepare-reviewer-handler.cjs").prepare(dir, proposalOutput()), /helpers changed/);
      assert.deepEqual(names.map(name => fs.readFileSync(path.join(dir, name), "utf8")), before);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }
});

test("both daemon configurations preserve native labels and reviewer API failures", async () => {
  for (const daemon of ["librarian", "maintainer"]) {
    const lock = fs.readFileSync(path.join(__dirname, `../workflows/${daemon}.lock.yml`), "utf8");
    const match = lock.match(/GH_AW_SAFE_OUTPUTS_HANDLER_CONFIG: ("[^\n]+")/);
    const config = JSON.parse(JSON.parse(match[1])).add_reviewer;
    assert.deepEqual(config.required_labels, [daemon]);
    for (const labels of [[daemon], ["wrong-label"]]) {
      const f = fixture();
      global.github.rest.issues.get = async () => ({ data: { labels: labels.map(name => ({ name })) } });
      const handler = await wrapReviewer(main)(config);
      assert.equal((await handler(f.message, f.map)).success, labels[0] === daemon);
      assert.equal(f.calls.length, labels[0] === daemon ? 1 : 0);
    }
  }
});


test("both PR publishers validate the exact native output before processing", () => {
  for (const name of ["librarian", "maintainer"]) {
    const root = path.resolve(__dirname, "../..");
    const source = fs.readFileSync(path.join(root, `.github/workflows/${name}.md`), "utf8");
    const lock = fs.readFileSync(path.join(root, `.github/workflows/${name}.lock.yml`), "utf8");
    const job = lock.split("  safe_outputs:\n")[1].split(/^  [a-z_]+:$/m)[0];
    const script = "node .github/agent-delivery/prepare-reviewer-handler.cjs";
    const step = "name: Resolve reviewer target using the native published PR map";
    const nativeOutput = "GH_AW_AGENT_OUTPUT: ${{ steps.setup-agent-output-env.outputs.GH_AW_AGENT_OUTPUT }}";
    assert.equal(source.split(script).length, 2, name);
    assert.equal(job.split(script).length, 2, name);
    assert(job.indexOf("name: Checkout repository") < job.indexOf(step), name);
    assert(job.indexOf(step) < job.indexOf("name: Process Safe Outputs"), name);
    assert(source.includes(nativeOutput), name);
    assert(job.slice(job.indexOf(step), job.indexOf("name: Process Safe Outputs")).includes(nativeOutput), name);
    assert.match(job.slice(job.indexOf(step), job.indexOf(script)), /contains\(needs.agent.outputs.output_types, 'create_pull_request'\)/);
  }
});
