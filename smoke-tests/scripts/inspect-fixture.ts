import { appendFile } from "node:fs/promises"
import { ContractError } from "../checks/lib/contract"
import { discoverFixture } from "../checks/lib/fixture-discovery"

try {
  const fixture = await discoverFixture(process.env)
  if (process.env.GITHUB_STEP_SUMMARY) {
    // Every value has been validated as a UUID, the fixed title, or an integer.
    const entries = Object.entries(fixture).map(([key, value]) => `${key}=${value}`).join("\n")
    await appendFile(process.env.GITHUB_STEP_SUMMARY, `## Verified Immich smoke fixture\n\n\`\`\`text\n${entries}\n\`\`\`\n`)
  }
  console.log(JSON.stringify(fixture, null, 2))
} catch (error) {
  console.error(error instanceof ContractError ? error.message : "Fixture inspection failed unexpectedly.")
  process.exitCode = 1
}
