import { ContractError, fixtureConfig, validateAlbum, validateUser, type Environment } from "./contract"
import { REQUEST_TIMEOUT_MS, type Fetch } from "./run"

const GUMNUT_ORIGIN = "https://api.gumnut.ai"
const ADAPTER_ORIGIN = "https://immich-adapter-docker.onrender.com"
const ALBUM_NAME = "Architecture"
const DEMO_EMAIL = "demo@gumnut.ai"
const MAX_BODY_BYTES = 1024 * 1024
const ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

function requireValue(ok: unknown, field: string): asserts ok {
  if (!ok) throw new ContractError(field, "fixture verification failed")
}
function object(value: unknown, field: string): Record<string, unknown> {
  requireValue(value !== null && typeof value === "object" && !Array.isArray(value), field)
  return value as Record<string, unknown>
}

// Gumnut's canonical IDs use Python shortuuid's default, sorted alphabet.
// Decode only its fixed-width UUID representation, never arbitrary base-57 IDs.
export function gumnutIdToUuid(value: unknown, prefix: "intuser" | "album" | "lib"): string {
  requireValue(typeof value === "string" && value.startsWith(`${prefix}_`), "identifier")
  const encoded = value.slice(prefix.length + 1)
  requireValue(encoded.length === 22, "identifier")
  let number = 0n
  for (const char of encoded) {
    const digit = ALPHABET.indexOf(char)
    requireValue(digit >= 0, "identifier")
    number = number * 57n + BigInt(digit)
  }
  requireValue(number < (1n << 128n), "identifier")
  const hex = number.toString(16).padStart(32, "0")
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

async function readJson(url: string, field: string, headers: Record<string, string>, fetcher: Fetch, timeoutMs: number) {
  const controller = new AbortController()
  let timer: ReturnType<typeof setTimeout> | undefined
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      reject(new ContractError(field, "timeout"))
      controller.abort()
    }, timeoutMs)
  })
  try {
    return await Promise.race([deadline, (async () => {
      let response: Response | undefined
      let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
      try {
        try {
          response = await fetcher(url, {
            method: "GET", headers, redirect: "manual", signal: controller.signal,
          })
        } catch { throw new ContractError(field, "transport failure") }
        if (response.status !== 200) throw new ContractError(field, `HTTP status ${response.status}`)
        try {
          const length = response.headers.get("content-length")
          requireValue(length === null || (/^[0-9]+$/.test(length) && Number(length) <= MAX_BODY_BYTES), field)
          requireValue(response.body, field)
          reader = response.body.getReader()
          const decoder = new TextDecoder("utf-8", { fatal: true })
          let bytes = 0
          let text = ""
          while (true) {
            const chunk = await reader.read()
            if (chunk.done) break
            bytes += chunk.value.byteLength
            requireValue(bytes <= MAX_BODY_BYTES, field)
            text += decoder.decode(chunk.value, { stream: true })
          }
          text += decoder.decode()
          return JSON.parse(text) as unknown
        } catch {
          // Body and parse failures can embed credentials or payloads.
          // Never propagate the original exception or attach it as a cause.
          throw new ContractError(field, "body or JSON failure")
        }
      } finally {
        if (reader) void reader.cancel().catch(() => {})
        else if (response?.body) void response.body.cancel().catch(() => {})
      }
    })()])
  } finally {
    clearTimeout(timer)
    controller.abort()
  }
}

export async function discoverFixture(env: Environment, fetcher: Fetch = globalThis.fetch, timeoutMs = REQUEST_TIMEOUT_MS) {
  requireValue(env.IMMICH_SMOKE_API_KEY?.trim(), "IMMICH_SMOKE_API_KEY")
  const apiKey = env.IMMICH_SMOKE_API_KEY!.trim()
  const read = (origin: string, path: string, field: string) => readJson(`${origin}${path}`, field,
    origin === GUMNUT_ORIGIN ? { Authorization: `Bearer ${apiKey}` } : { "x-api-key": apiKey }, fetcher, timeoutMs)
  const user = object(await read(GUMNUT_ORIGIN, "/api/users/me", "Gumnut user"), "Gumnut user")
  requireValue(user.email === DEMO_EMAIL && user.is_active === true, "Gumnut identity")
  const userId = gumnutIdToUuid(user.id, "intuser")
  const adapterUser = object(await read(ADAPTER_ORIGIN, "/api/users/me", "adapter user"), "adapter user")
  requireValue(adapterUser.email === DEMO_EMAIL && adapterUser.id === userId, "adapter identity")

  // The endpoint defaults to live libraries and returns the complete array.
  const libraries = await read(GUMNUT_ORIGIN, "/api/libraries", "libraries")
  requireValue(Array.isArray(libraries) && libraries.length === 1, "libraries")
  const library = object(libraries[0], "library")
  gumnutIdToUuid(library.id, "lib")
  requireValue(library.user_id === user.id && library.role === "owner", "library ownership")
  requireValue(object(library.owner, "library owner").id === user.id, "library ownership")
  if ("owner_id" in library) requireValue(library.owner_id === user.id, "library ownership")
  requireValue(user.immich_library_id === null || user.immich_library_id === library.id, "library selection")
  const query = new URLSearchParams({ library_id: library.id as string, name: ALBUM_NAME, limit: "2" })
  const albums = object(await read(GUMNUT_ORIGIN, `/api/albums?${query}`, "albums"), "albums")
  requireValue(Array.isArray(albums.data) && albums.data.length === 1 && albums.has_more === false, "albums")
  const album = object(albums.data[0], "fixture album")
  requireValue(album.name === ALBUM_NAME, "fixture album name")
  requireValue(typeof album.asset_count === "number" && Number.isSafeInteger(album.asset_count) && album.asset_count > 0, "fixture album count")
  const fixture = {
    IMMICH_SMOKE_USER_ID: userId,
    IMMICH_SMOKE_ALBUM_ID: gumnutIdToUuid(album.id, "album"),
    IMMICH_SMOKE_ALBUM_NAME: ALBUM_NAME,
    IMMICH_SMOKE_ASSET_COUNT: String(album.asset_count),
  }
  const config = fixtureConfig(fixture)
  validateUser(adapterUser, config)
  validateAlbum(await read(ADAPTER_ORIGIN, `/api/albums/${config.albumId}`, "adapter album"), config)
  return fixture
}
