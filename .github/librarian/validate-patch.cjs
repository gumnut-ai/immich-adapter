// Enforce commit attribution on the exact reconstructed native publication.
const { execFileSync } = require("node:child_process");
const base = process.env.GITHUB_SHA;
if (!/^[a-f0-9]{40}$/.test(base || "")) throw Error("Missing immutable dispatch revision");
const identity = "gumnut-bot[bot]|333470196+gumnut-bot[bot]@users.noreply.github.com|gumnut-bot[bot]|333470196+gumnut-bot[bot]@users.noreply.github.com";
const commits = execFileSync("git", ["log", `${base}..HEAD`, "--format=%an|%ae|%cn|%ce"], {encoding:"utf8"}).trim();
if (commits.split("\n").filter(Boolean).some(commit => commit !== identity))
  throw Error("Unexpected proposal commit identity");
