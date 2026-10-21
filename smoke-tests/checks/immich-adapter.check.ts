import path from "node:path"
import { fileURLToPath } from "node:url"
import { CheckGroupV2, MultiStepCheck, SlackAlertChannel } from "checkly/constructs"
import { FIXTURE_KEYS, fixtureConfig } from "./lib/contract"
import { POLICY, requiredSecret, scheduleActive, slackChannelId } from "./lib/settings"

fixtureConfig(process.env)
const group = new CheckGroupV2("immich-adapter-production-smoke", {
  name: "Immich adapter production smoke",
  ...POLICY,
  activated: scheduleActive(process.env),
  locations: [...POLICY.locations],
  alertChannels: [SlackAlertChannel.fromId(slackChannelId(process.env))],
  environmentVariables: [
    { key: "IMMICH_SMOKE_API_KEY", value: requiredSecret("IMMICH_SMOKE_API_KEY"), locked: true, secret: true },
    ...FIXTURE_KEYS.map(key => ({ key, value: process.env[key]!, locked: true })),
  ],
  tags: ["production", "smoke", "immich-adapter"],
})

new MultiStepCheck("immich-adapter-read-contract", {
  name: "Immich adapter user and album read contract",
  group,
  code: { entrypoint: path.join(path.dirname(fileURLToPath(import.meta.url)), "read-contract.spec.ts") },
})
