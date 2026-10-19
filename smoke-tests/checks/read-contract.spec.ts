import { test } from "@playwright/test"
import { runContract } from "./lib/run"

test("authenticated user and seeded album read contract", async () => {
  test.setTimeout(25_000)
  await runContract(process.env)
})
