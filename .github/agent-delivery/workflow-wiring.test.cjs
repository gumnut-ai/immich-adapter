const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

for (const daemon of ["librarian", "maintainer"]) {
  test(`${daemon} installs the shared repair before native safe outputs from trusted source`, () => {
    const source = fs.readFileSync(path.join(__dirname, `../workflows/${daemon}.md`), "utf8");
    const lock = fs.readFileSync(path.join(__dirname, `../workflows/${daemon}.lock.yml`), "utf8");
    const repair = "run: node .github/agent-delivery/prepare-reviewer-handler.cjs";
    assert.equal(source.split(repair).length, 2);
    assert.equal(lock.split(repair).length, 2);
    assert.match(source, /safe-outputs:[\s\S]+?  steps:\n    - name: Resolve reviewer target[\s\S]+?if: contains\(needs.agent.outputs.output_types, 'create_pull_request'\)/);
    const safe = lock.slice(lock.indexOf("  safe_outputs:\n"), lock.indexOf("  verify_delivery:\n"));
    assert.ok(safe.includes(repair));
    assert.ok(safe.indexOf("uses: github/gh-aw/actions/setup@c35393777e5604a63721d09512263b1383301d4f") < safe.indexOf(repair));
    assert.ok(safe.indexOf("ref: ${{ github.sha }}") < safe.indexOf(repair));
    assert.ok(safe.indexOf(repair) < safe.indexOf("name: Process Safe Outputs"));
    assert.match(safe, /if: contains\(needs.agent.outputs.output_types, 'create_pull_request'\)\n        run: node/);
    const match = safe.match(/GH_AW_SAFE_OUTPUTS_HANDLER_CONFIG: ("[^\n]+")/);
    const config = JSON.parse(JSON.parse(match[1]));
    assert.equal(config.add_reviewer.max, 1);
    assert.equal(config.add_reviewer.target, "*");
    assert.deepEqual(config.add_reviewer.required_labels, [daemon]);
    assert.match(lock, /"compiler_version":"v0.89.21"/);
    for (const login of ["CharlieHelps", "CharlieCreates", "github-actions", "gumnut-bot", "chatgpt-codex-connector", "Copilot"])
      assert.ok(source.includes(`\`${login}\``), login);
  });
}


for (const daemon of ["librarian", "maintainer"]) {
  test(`${daemon} denies reviewer-only safe-output publication before native processing`, () => {
    for (const extension of ["md", "lock.yml"]) {
      const text = fs.readFileSync(path.join(__dirname, `../workflows/${daemon}.${extension}`), "utf8");
      const safe = text.slice(text.indexOf("  safe_outputs:\n"));
      const match = safe.match(/^    if: (.+)((?:\n      .+)*)/m);
      const condition = match[1] === ">" ? match[2].trim().split(/\n\s+/).join(" ") : match[1];
      const guard = "(!contains(needs.agent.outputs.output_types, 'add_reviewer') || contains(needs.agent.outputs.output_types, 'create_pull_request'))";
      assert.ok(condition.includes(guard));
      assert.ok(condition.includes("needs.agent.result == 'success'"));
      assert.ok(condition.includes("needs.detection.outputs.detection_success == 'true'"));
      for (const [types, allowed] of [
        ["add_reviewer", false], ["noop,add_reviewer", false],
        ["create_pull_request,add_reviewer", true], ["noop", true],
        ["create_pull_request", true],
      ]) {
        const expression = condition
          .replaceAll("needs.agent.result", JSON.stringify("success"))
          .replaceAll("needs.detection.outputs.detection_success", JSON.stringify("true"))
          .replaceAll("needs.detection.result", JSON.stringify("success"))
          .replaceAll("needs.agent.outputs.output_types", JSON.stringify(types));
        const actual = Function("contains", "cancelled", `return (${expression})`)((value, part) => value.includes(part), () => false);
        assert.equal(actual, allowed, `${extension}: ${types}`);
      }
    }
  });
}
