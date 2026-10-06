const { isHumanReviewer } = require("./human-reviewer.cjs");

// v0.89.21's add_reviewer ignores resolvedTemporaryIds. Resolve only this
// activation's newly published PR, then let the native handler own the API call.
function wrapReviewer(main) {
  return async (config) => {
    const native = await main(config);
    return async (message, resolvedTemporaryIds) => {
      const id = typeof message.pull_request_number === "string"
        ? message.pull_request_number.replace(/^#/, "").toLowerCase() : "";
      const target = Object.hasOwn(resolvedTemporaryIds || {}, id)
        ? resolvedTemporaryIds[id] : null;
      const repository = `${context.repo.owner}/${context.repo.repo}`;
      if (!/^aw_[A-Za-z0-9_]{3,12}$/.test(id) ||
          target?.repo !== repository || !Number.isSafeInteger(target?.number) ||
          target.number <= 0 || (message.repo && message.repo !== repository)) {
        return { success: false, error: "Reviewer target must be this activation's published PR" };
      }
      if (!Array.isArray(message.reviewers) || message.reviewers.length !== 1 ||
          !isHumanReviewer(message.reviewers[0]) || message.team_reviewers?.length) {
        return { success: false, error: "Exactly one eligible human reviewer is required" };
      }
      return native({ ...message, pull_request_number: target.number }, resolvedTemporaryIds);
    };
  };
}

module.exports = { wrapReviewer };
