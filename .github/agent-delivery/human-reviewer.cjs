const AUTOMATION = new Set([
  "charliehelps", "charliecreates", "github-actions", "gumnut-bot",
  "chatgpt-codex-connector", "copilot",
]);

function isHumanReviewer(login) {
  return typeof login === "string" && login.length > 0 &&
    !login.toLowerCase().endsWith("[bot]") && !AUTOMATION.has(login.toLowerCase());
}

module.exports = { isHumanReviewer };
