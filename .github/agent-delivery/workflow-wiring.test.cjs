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
    assert.match(source, /safe-outputs:[\s\S]+?  steps:\n[\s\S]+?    - name: Resolve reviewer target[\s\S]+?if: contains\(needs.agent.outputs.output_types, 'create_pull_request'\)/);
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

for(const daemon of ['librarian','maintainer']) {
  test(`${daemon} bounds existing updates with identical native scope and frozen admission`,()=>{
    const source=fs.readFileSync(path.join(__dirname,`../workflows/${daemon}.md`),'utf8');
    const lock=fs.readFileSync(path.join(__dirname,`../workflows/${daemon}.lock.yml`),'utf8');
    const safe=lock.slice(lock.indexOf('  safe_outputs:\n'),lock.indexOf('  verify_delivery:\n'));
    const match=safe.match(/GH_AW_SAFE_OUTPUTS_HANDLER_CONFIG: ("[^\n]+")/);
    const config=JSON.parse(JSON.parse(match[1]));
    assert.equal(config.push_to_pull_request_branch.max,1);
    assert.equal(config.push_to_pull_request_branch.fallback_as_pull_request,false);
    assert.equal(config.push_to_pull_request_branch.signed_commits,false);
    for(const field of ['allowed_files','protected_files','protected_files_policy','protect_top_level_dot_folders'])
      assert.deepEqual(config.push_to_pull_request_branch[field],config.create_pull_request[field],field);
    assert(safe.indexOf('Guard native publication')<safe.indexOf('name: Process Safe Outputs'));
    assert(safe.includes('validated.candidateHead!==proof.head'));
    assert(source.indexOf('name: Record the exact verified candidate')>source.indexOf('post-steps:'));
    assert(source.includes('git fetch origin "$ADMITTED_HEAD:refs/remotes/origin/$branch"'));
    assert(source.indexOf('name: Retain immutable native admission')<source.indexOf('post-steps:'));
    assert(source.includes('PUSH_SHA: ${{ needs.safe_outputs.outputs.push_commit_sha }}'));
    assert(source.includes('name: Read immutable native admission'));
  });
}

for (const daemon of ['librarian','maintainer']) {
  test(`${daemon} checks out trusted dispatch for noop validation without replacing native publication checkout`,()=>{
    for (const extension of ['md','lock.yml']) {
      const text=fs.readFileSync(path.join(__dirname,`../workflows/${daemon}.${extension}`),'utf8');
      const start=text.indexOf('- name: Checkout trusted dispatch for non-publication');
      assert(start>=0);
      const following=text.slice(start).search(/\n\s+- name:/);
      assert(following>0);
      const step=text.slice(start,start+following);
      assert(step.includes('uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1'));
      assert(step.includes('ref: ${{ github.sha }}'));
      assert(step.includes('persist-credentials: false'));
      assert(step.includes('fetch-depth: 0'));
      assert(start<text.indexOf('- name: Guard native publication and complete candidate scope'));
      const condition=step.match(/\n\s+if: (.+)/)[1].replace(/^"|"$/g,'');
      for (const [types,expected] of [
        ['noop',true],['report_incomplete',true],['create_pull_request',false],
        ['push_to_pull_request_branch',false],['create_pull_request,add_reviewer',false],
        ['noop,create_pull_request',false],['noop,push_to_pull_request_branch',false],
      ]) {
        const expression=condition.replaceAll('needs.agent.outputs.output_types',JSON.stringify(types));
        assert.equal(Function('contains',`return (${expression})`)((value,part)=>value.includes(part)),expected,`${extension}: ${types}`);
      }
      if(extension==='lock.yml') {
        const safe=text.slice(text.indexOf('  safe_outputs:\n'),text.indexOf('  verify_delivery:\n'));
        const native=safe.slice(safe.indexOf('- name: Checkout repository\n'),safe.indexOf('- name: Checkout trusted dispatch for non-publication'));
        assert(native.includes("contains(needs.agent.outputs.output_types, 'create_pull_request')"));
        assert(native.includes("contains(needs.agent.outputs.output_types, 'push_to_pull_request_branch')"));
        assert(native.includes('persist-credentials: true'));
        assert(native.includes('GIT_TOKEN: ${{ steps.safe-outputs-app-token.outputs.token }}'));
        assert(native.includes('configure_git_credentials.sh'));
      }
    }
  });
}
