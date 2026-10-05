const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");

function prepare(actionsDir) {
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

if (require.main === module)
  prepare(path.join(process.env.RUNNER_TEMP, "gh-aw/actions"));
module.exports = { prepare };
