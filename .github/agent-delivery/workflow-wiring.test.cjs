const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const workflows = [["librarian", ["librarian"]], ["maintainer", ["maintainer"]]];
const validateProposal = true;

function publisher(text) {
  return text.split("\n  safe_outputs:\n")[1]?.split(/\n  [a-z_]+:\n/)[0] || "";
}
function actionStep(text) {
  return text.match(/^( +)- name: Resolve reviewer target using the native published PR map\n[\s\S]*?(?=^\1- |^ {0,4}[a-z][\w-]*:|(?![\s\S]))/m)?.[0] || "";
}

for (const [name, labels] of workflows) {
  test(`${name} installs the pinned shared handoff after trusted setup and checkout`, () => {
    const directory = path.join(__dirname, "../workflows");
    const source = fs.readFileSync(path.join(directory, `${name}.md`), "utf8");
    const lock = fs.readFileSync(path.join(directory, `${name}.lock.yml`), "utf8");
    const job = publisher(lock);
    const reference = /uses: gumnut-ai\/automation\/actions\/daemon-delivery@([a-f0-9]{40})(?=\s|$)/g;
    const sourceRefs = [...source.matchAll(reference)];
    const jobRefs = [...job.matchAll(reference)];
    assert.equal(sourceRefs.length, 1);
    assert.equal(jobRefs.length, 1);
    assert.notEqual(sourceRefs[0][1], "0".repeat(40), "replace the staging pin before deployment");
    assert.equal(jobRefs[0][1], sourceRefs[0][1]);
    for (const text of [source, job]) {
      const step = actionStep(text);
      assert.match(step, /if: contains\(needs.agent.outputs.output_types, 'create_pull_request'\)/);
      assert.match(step, /uses: gumnut-ai\/automation\/actions\/daemon-delivery@[a-f0-9]{40}/);
      if (validateProposal) {
        assert.match(step, /validate-proposal: true/);
        assert.ok(step.includes("GH_AW_AGENT_OUTPUT: ${{ steps.setup-agent-output-env.outputs.GH_AW_AGENT_OUTPUT }}"));
      } else {
        assert.doesNotMatch(step, /validate-proposal: true/);
      }
      assert.doesNotMatch(text, /node \.github\/agent-delivery\/prepare-reviewer-handler\.cjs/);
    }
    const actionIndex = jobRefs[0].index;
    for (const marker of ["uses: github/gh-aw/actions/setup@c35393777e5604a63721d09512263b1383301d4f", "name: Checkout repository", "ref: ${{ github.sha }}"]) {
      assert.ok(job.indexOf(marker) >= 0, marker);
      assert.ok(job.indexOf(marker) < actionIndex, marker);
    }
    assert.ok(actionIndex < job.indexOf("name: Process Safe Outputs"));
    const match = job.match(/GH_AW_SAFE_OUTPUTS_HANDLER_CONFIG: ("[^\n]+")/);
    assert.ok(match, "native safe-output handler configuration");
    const config = JSON.parse(JSON.parse(match[1]));
    assert.equal(config.add_reviewer.max, 1);
    assert.equal(config.add_reviewer.target, "*");
    assert.deepEqual(config.add_reviewer.required_labels, labels);
    assert.match(lock, /"compiler_version":"v0.89.21"/);

  });

  test(`${name} rejects reviewer-only publication before native processing`, () => {
    for (const extension of ["md", "lock.yml"]) {
      const text = fs.readFileSync(path.join(__dirname, `../workflows/${name}.${extension}`), "utf8");
      const admission = (publisher(text).match(/^    if: (.+(?:\n {6}.+)*)/m)?.[1] || "").replace(/\s+/g, " ");
      const guard = "(!contains(needs.agent.outputs.output_types, 'add_reviewer') || contains(needs.agent.outputs.output_types, 'create_pull_request'))";
      assert.ok(admission.includes(guard));
      assert.ok(admission.includes("needs.agent.result == 'success'"));
      assert.ok(admission.includes("needs.detection.outputs.detection_success == 'true'"));
      for (const [types, allowed] of [["add_reviewer", false], ["noop,add_reviewer", false], ["create_pull_request,add_reviewer", true], ["noop", true], ["create_pull_request", true]]) {
        assert.equal(vm.runInNewContext(guard, {
          contains: (value, item) => value.includes(item),
          needs: { agent: { outputs: { output_types: types } } },
        }), allowed, `${extension}: ${types}`);
      }
    }
  });

}
