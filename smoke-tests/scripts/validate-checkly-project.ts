import path from "node:path"
import { fileURLToPath } from "node:url"
import { Diagnostics, Session } from "checkly/constructs"
// Pinned CLI internals perform parsing, validation and bundling without uploads
// or running checks. Revalidate these imports when changing the Checkly pin.
import { loadChecklyConfig } from "../node_modules/checkly/dist/services/checkly-config-loader.js"
import { Bundler } from "../node_modules/checkly/dist/services/check-parser/bundler.js"
import { parseProject } from "../node_modules/checkly/dist/services/project-parser.js"

const directory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const runtime = { name: "2026.04", dependencies: { "@playwright/test": "1.62.1" }, multiStepSupport: true }
// Placeholders exist only in this no-traffic validator. No production fallback.
Object.assign(process.env, {
  CHECKLY_SECRET_IMMICH_SMOKE_API_KEY: "validation-placeholder",
  CHECKLY_SLACK_CHANNEL_ID: "1",
  SMOKE_SCHEDULE_ACTIVE: "false",
  IMMICH_SMOKE_USER_ID: "11111111-1111-4111-8111-111111111111",
  IMMICH_SMOKE_ALBUM_ID: "22222222-2222-4222-8222-222222222222",
  IMMICH_SMOKE_ALBUM_NAME: "Example smoke album",
  IMMICH_SMOKE_ASSET_COUNT: "1",
})
const { config, constructs } = await loadChecklyConfig(directory, ["checkly.config.ts"])
const project = await parseProject({
  directory, projectLogicalId: config.logicalId, projectName: config.projectName,
  repoUrl: config.repoUrl, includeTestOnlyChecks: true,
  checkMatch: config.checks?.checkMatch,
  browserCheckMatch: config.checks?.browserChecks?.testMatch,
  multiStepCheckMatch: config.checks?.multiStepChecks?.testMatch,
  ignoreDirectoriesMatch: config.checks?.ignoreDirectoriesMatch,
  checkDefaults: config.checks, browserCheckDefaults: config.checks?.browserChecks,
  availableRuntimes: { [runtime.name]: runtime }, defaultRuntimeId: runtime.name,
  verifyRuntimeDependencies: true, checklyConfigConstructs: constructs,
})
const diagnostics = new Diagnostics()
await project.validate(diagnostics)
for (const diagnostic of diagnostics.observations) {
  console.log(`${diagnostic.isFatal() ? "error" : "notice"}: ${diagnostic.title}: ${diagnostic.message}`)
}
if (diagnostics.isFatal()) process.exit(1)
const bundler = await Bundler.createForWorkspace(Session.workspace.unwrap())
const bundle = await project.bundle(bundler)
if (Object.keys(bundle.data.check).length !== 1 || !bundle.data.check["immich-adapter-read-contract"]) {
  throw new Error("Expected exactly the Immich adapter read contract check")
}
console.log("Validated and bundled exactly 1 Checkly check locally.")
