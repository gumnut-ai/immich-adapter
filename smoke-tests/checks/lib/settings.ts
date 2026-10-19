import { AlertEscalationBuilder, Frequency, RetryStrategyBuilder } from "checkly/constructs"
import { secret } from "checkly/util"
import { ContractError, type Environment } from "./contract"

export function scheduleActive(env: Environment) {
  const value = env.SMOKE_SCHEDULE_ACTIVE ?? "false"
  if (value !== "true" && value !== "false") throw new ContractError("SMOKE_SCHEDULE_ACTIVE", "expected true or false")
  return value === "true"
}
export function slackChannelId(env: Environment) {
  const value = env.CHECKLY_SLACK_CHANNEL_ID ?? ""
  if (!/^[1-9][0-9]*$/.test(value) || !Number.isSafeInteger(Number(value))) {
    throw new ContractError("CHECKLY_SLACK_CHANNEL_ID", "expected positive safe integer")
  }
  return Number(value)
}
export function requiredSecret(name: string) {
  let value: string
  try { value = secret(name).trim() }
  catch { throw new ContractError("IMMICH_SMOKE_API_KEY", "missing deployment secret") }
  if (!value) throw new ContractError("IMMICH_SMOKE_API_KEY", "empty deployment secret")
  return value
}
export const POLICY = {
  muted: false,
  frequency: Frequency.EVERY_1H,
  locations: ["us-east-1"],
  runtimeId: "2026.04",
  retryStrategy: RetryStrategyBuilder.noRetries(),
  alertEscalationPolicy: AlertEscalationBuilder.runBasedEscalation(1, { amount: 0, interval: 5 }),
} as const
