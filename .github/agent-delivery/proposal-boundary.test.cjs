const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { spawnSync } = require("node:child_process");
const { prepare, validateProposal } = require("./prepare-reviewer-handler.cjs");
const setup = process.env.GH_AW_SETUP_DIR;
if (!setup) throw Error("GH_AW_SETUP_DIR must identify pinned v0.89.21 setup/js");

function output() {
  return { errors: [], items: [
    { type: "create_pull_request", temporary_id: "aw_proposal" },
    { type: "add_reviewer", pull_request_number: "aw_proposal", reviewers: ["maintainer"] },
  ] };
}
function helpers(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "proposal-boundary-"));
  fs.cpSync(setup, dir, { recursive: true });
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}
function nativeFiles(dir) {
  return ["add_reviewer.cjs", "safe_output_handler_manager.cjs"].map(name =>
    fs.readFileSync(path.join(dir, name), "utf8"));
}
function invalidOutputs() {
  const result = [{}, { items: [] }, { items: [null] }, { items: [output().items[0]] }];
  for (const type of ["noop", "report_incomplete", "missing_tool", "missing_data"])
    result.push({ items: [...output().items, { type }] });
  for (const mutate of [
    x => x.errors = ["Line 3: Unexpected output type 'report_incomplete'"],
    x => x.errors = ["Line 3: Too many items of type 'create_pull_request'"],
    x => x.errors = "invalid error envelope",
    x => x.items.push(x.items[0]),
    x => x.items.push(x.items[1]),
    x => delete x.items[0].temporary_id,
    x => x.items[1].pull_request_number = 7,
    x => x.items[1].pull_request_number = "aw_other",
    x => x.items[1].repo = "other/repo",
    x => x.items[0].repo = "other/repo",
    x => x.items[1].reviewers = [],
    x => x.items[1].reviewers = ["maintainer", "other"],
    x => x.items[1].reviewers = ["CharlieHelps"],
    x => x.items[1].reviewers = ["someone[BoT]"],
    x => x.items[1].reviewers = ["not/a/login"],
    x => x.items[1].team_reviewers = ["team"],
  ]) { const x = output(); mutate(x); result.push(x); }
  return result;
}

test("incomplete declarations fail before helpers change or a native write handler runs", async t => {
  const dir = helpers(t), before = nativeFiles(dir);
  let writes = 0;
  for (const declaration of invalidOutputs()) {
    await assert.rejects(async () => {
      prepare(dir, declaration, "owner/repo");
      const { processMessages } = require(path.join(dir, "safe_output_handler_manager.cjs"));
      await processMessages(new Map([["create_pull_request", async () => { writes++; }]]), declaration.items);
    });
    assert.deepEqual(nativeFiles(dir), before);
    assert.equal(writes, 0);
  }
});

test("completed proposal uses native ordering, temporary map and exactly one write per handler", async t => {
  global.core = { info() {}, warning() {}, error() {}, debug() {} };
  for (const reversed of [false, true]) {
    const dir = helpers(t), declaration = output();
    declaration.items[1].pull_request_number = "#AW_Proposal";
    if (reversed) declaration.items.reverse();
    prepare(dir, declaration, "owner/repo");
    const { processMessages } = require(path.join(dir, "safe_output_handler_manager.cjs"));
    const calls = [];
    const handlers = new Map([
      ["create_pull_request", async () => {
        calls.push("pr");
        return { success: true, number: 7, repo: "owner/repo", temporaryId: "aw_proposal" };
      }],
      ["add_reviewer", async (message, map) => {
        calls.push("review");
        assert.deepEqual(map.aw_proposal, { repo: "owner/repo", number: 7 });
        return { success: true };
      }],
    ]);
    const result = await processMessages(handlers, declaration.items);
    assert.deepEqual(calls, ["pr", "review"]);
    assert(result.results.every(item => item.success));
  }
});

test("CLI requires readable valid native output and fails before native helper preparation", t => {
  const dir = helpers(t), before = nativeFiles(dir);
  const file = path.join(dir, "output.json");
  for (const content of [undefined, "invalid JSON", JSON.stringify({ items: [output().items[0]] }), JSON.stringify({ ...output(), errors: ["Invalid extra declaration"] })]) {
    if (content !== undefined) fs.writeFileSync(file, content);
    const env = { PATH: process.env.PATH, RUNNER_TEMP: dir, GITHUB_REPOSITORY: "owner/repo" };
    if (content !== undefined) env.GH_AW_AGENT_OUTPUT = file;
    const run = spawnSync(process.execPath, [path.join(__dirname, "prepare-reviewer-handler.cjs")], { env, encoding: "utf8" });
    assert.notEqual(run.status, 0);
    assert.deepEqual(nativeFiles(dir), before);
  }
});

test("CLI prepares the pinned handlers from the exact native output path", t => {
  const dir = helpers(t);
  const actions = path.join(dir, "gh-aw/actions");
  fs.mkdirSync(path.dirname(actions), { recursive: true });
  fs.cpSync(setup, actions, { recursive: true });
  const file = path.join(dir, "output.json");
  fs.writeFileSync(file, JSON.stringify(output()));
  const run = spawnSync(process.execPath, [path.join(__dirname, "prepare-reviewer-handler.cjs")], {
    env: { PATH: process.env.PATH, RUNNER_TEMP: dir, GITHUB_REPOSITORY: "owner/repo", GH_AW_AGENT_OUTPUT: file },
    encoding: "utf8",
  });
  assert.equal(run.status, 0, run.stderr);
  assert.notDeepEqual(nativeFiles(actions), nativeFiles(dir));
});

test("preflight accepts either declaration order and an explicit same-repository target", () => {
  const declaration = output();
  declaration.items.forEach(item => item.repo = "owner/repo");
  validateProposal(declaration, "owner/repo");
  declaration.items.reverse();
  validateProposal(declaration, "owner/repo");
});
