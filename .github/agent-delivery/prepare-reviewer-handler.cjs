const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");

const { isHumanReviewer } = require("./human-reviewer.cjs");

// Native handlers apply items independently. Validate the complete proposal
// before any handler can publish; delivery readback cannot undo a write.
function validateProposal(output, repository) {
  if (!Array.isArray(output?.errors) || output.errors.length !== 0)
    throw Error("Native output collection must have no validation errors");
  const items = output?.items;
  if (!Array.isArray(items) || (items.length !== 1 && items.length !== 2) ||
      items.some(item => !item || typeof item !== "object"))
    throw Error("Expected one PR proposal with at most one human reviewer");
  const proposals = items.filter(item => item.type === "create_pull_request");
  const reviews = items.filter(item => item.type === "add_reviewer");
  if (proposals.length !== 1 || reviews.length > 1 ||
      items.length !== proposals.length + reviews.length)
    throw Error("Expected one PR proposal with at most one human reviewer");
  if (items.some(item => item.repo && item.repo !== repository))
    throw Error("Proposal and reviewer must target this repository");
  const proposal = proposals[0], review = reviews[0];
  const normalize = id => typeof id === "string" ? id.replace(/^#/, "").toLowerCase() : "";
  const id = normalize(proposal.temporary_id);
  if (!/^aw_[a-z0-9_]{3,12}$/.test(id))
    throw Error("Invalid proposal temporary ID");
  // Preserve a useful PR when policy cannot identify a human; delivery still
  // reports the unresolved handoff rather than claiming success.
  if (!review) return;
  if (normalize(review.pull_request_number) !== id)
    throw Error("Reviewer target must be this proposal's temporary ID");
  if (!Array.isArray(review.reviewers) || review.reviewers.length !== 1 ||
      !/^[a-zA-Z0-9-]+$/.test(review.reviewers[0]) ||
      !isHumanReviewer(review.reviewers[0]) || review.team_reviewers?.length)
    throw Error("Exactly one eligible human reviewer is required");
}

function prepare(actionsDir, output, repository = process.env.GITHUB_REPOSITORY) {
  validateProposal(output, repository);
  const file = path.join(actionsDir, "add_reviewer.cjs");
  const source = fs.readFileSync(file, "utf8");
  const anchor = "module.exports = { main };";
  const managerFile = path.join(actionsDir, "safe_output_handler_manager.cjs");
  const manager = fs.readFileSync(managerFile, "utf8");
  const dependency = 'message.type === "create_issue" ? extractTemporaryIdReferences({ blocked_by: message.blocked_by }) : new Set()';
  // Fail before either write if the complete pinned helpers drift. Anchors
  // alone cannot establish compatibility with the reviewed native version.
  const hash = text => createHash("sha256").update(text).digest("hex");
  if (hash(source) !== "e9a10677c47acbc8f89958815f2ba1b120e9fed9e321523cdad04833f37fc3a1" ||
      hash(manager) !== "e9ee47cb0e09829e006b3226e1eae38d524a281e41934346a63d2a4526eccbf5")
    throw Error("Pinned v0.89.21 reviewer helpers changed");
  if (source.split(anchor).length !== 2 || manager.split(dependency).length !== 2)
    throw Error("Pinned reviewer handler changed");
  const wrapper = path.join(__dirname, "reviewer-handler.cjs");
  fs.writeFileSync(file, source.replace(anchor,
    `module.exports = { main: require(${JSON.stringify(wrapper)}).wrapReviewer(main) };`));
  // Extend the native dependency sort, retaining its stable ordering and map.
  fs.writeFileSync(managerFile, manager.replace(dependency,
    'message.type === "add_reviewer" ? extractTemporaryIdReferences({ pull_request_number: message.pull_request_number }) : ' + dependency));
}

if (require.main === module) {
  if (!process.env.GH_AW_AGENT_OUTPUT) throw Error("Missing native agent output path");
  const output = JSON.parse(fs.readFileSync(process.env.GH_AW_AGENT_OUTPUT, "utf8"));
  prepare(path.join(process.env.RUNNER_TEMP, "gh-aw/actions"), output);
}
module.exports = { prepare, validateProposal };
