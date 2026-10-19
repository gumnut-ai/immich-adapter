import { defineConfig } from "checkly"
import { POLICY, scheduleActive } from "./checks/lib/settings"

export default defineConfig({
  projectName: "Immich Adapter Production Smoke Testing",
  logicalId: "immich-adapter-production-smoke-testing",
  repoUrl: "https://github.com/gumnut-ai/immich-adapter",
  checks: {
    activated: scheduleActive(process.env),
    muted: POLICY.muted,
    frequency: POLICY.frequency,
    locations: [...POLICY.locations],
    runtimeId: POLICY.runtimeId,
    checkMatch: "checks/**/*.check.ts",
    tags: ["production", "smoke", "immich-adapter"],
  },
  cli: { retries: 0, runLocation: "us-east-1" },
})
