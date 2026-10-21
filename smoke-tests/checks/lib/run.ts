import { ContractError, runtimeConfig, validateAlbum, validateUser, type Environment } from "./contract"

const ORIGIN = "https://immich-adapter-docker.onrender.com"
export const REQUEST_TIMEOUT_MS = 10_000
export type Fetch = typeof globalThis.fetch

async function readJson(path: string, field: string, apiKey: string, fetcher: Fetch, timeoutMs: number) {
  // Checkly's Multistep fetch bridge does not accept AbortSignal. A local
  // deadline bounds both the header wait and body read without that primitive.
  let timer: ReturnType<typeof setTimeout> | undefined
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      reject(new ContractError(field, "timeout"))
    }, timeoutMs)
  })
  try {
    return await Promise.race([deadline, (async () => {
      let response: Response
      try {
        response = await fetcher(`${ORIGIN}${path}`, {
          method: "GET", headers: { "x-api-key": apiKey }, redirect: "manual",
        })
      } catch { throw new ContractError(field, "transport failure") }
      let status: unknown
      try { status = response.status }
      catch { throw new ContractError(field, "invalid response status") }
      if (typeof status !== "number" || !Number.isInteger(status) || status < 100 || status > 599) {
        throw new ContractError(field, "invalid response status")
      }
      if (status !== 200) {
        // Never inspect an error body or a redirect destination.
        throw new ContractError(field, `HTTP status ${status}`)
      }
      let text: string
      try { text = await response.text() }
      catch { throw new ContractError(field, "body read failure") }
      // Checkly's response.json bridge can hang; parse the fetched text locally.
      try { return JSON.parse(text) as unknown }
      catch { throw new ContractError(field, "invalid JSON") }
    })()])
  } finally { clearTimeout(timer) }
}

export async function runContract(env: Environment, fetcher: Fetch = globalThis.fetch, timeoutMs = REQUEST_TIMEOUT_MS) {
  // Validate the complete operator-provided fixture before making either request.
  const config = runtimeConfig(env)
  validateUser(await readJson("/api/users/me", "user", config.apiKey, fetcher, timeoutMs), config)
  validateAlbum(await readJson(`/api/albums/${config.albumId}`, "album", config.apiKey, fetcher, timeoutMs), config)
}
