import { test } from "node:test"
import assert from "node:assert/strict"
import { spawnSync } from "node:child_process"
import { discoverFixture, gumnutIdToUuid } from "../checks/lib/fixture-discovery"
import type { Fetch } from "../checks/lib/run"

const secret = "example-secret-never-log"
const userId = "11111111-1111-4111-8111-111111111111"
const albumId = "22222222-2222-4222-8222-222222222222"
const upstreamUserId = "intuser_546fMQ5WFDaZtG7F87gYX8"
const libraryId = "lib_2222222222222222222222"
const date = "2026-01-01T00:00:00Z"
const env = { IMMICH_SMOKE_API_KEY: secret }
function payloads(): unknown[] {
  const adapterUser = {
    id: userId, clusterGroupId: userId, email: "demo@gumnut.ai", name: "Example User",
    isAdmin: false, status: "active", avatarColor: "primary", profileImagePath: "",
    profileChangedAt: date, createdAt: date, updatedAt: date,
  }
  return [
    { id: upstreamUserId, email: "demo@gumnut.ai", is_active: true, immich_library_id: null },
    adapterUser,
    [{ id: libraryId, user_id: upstreamUserId, role: "owner", owner: { id: upstreamUserId } }],
    { data: [{ id: "album_86BKgn8zPuCmdySZTr5nk8", name: "Architecture", asset_count: 91 }], has_more: false },
    {
      id: albumId, albumName: "Architecture", assetCount: 91, shared: false, hasSharedLink: false,
      albumUsers: [{ role: "owner", user: adapterUser }], createdAt: date, updatedAt: date,
      startDate: date, endDate: date, lastModifiedAssetTimestamp: null, albumThumbnailAssetId: null,
    },
  ]
}
function mock(values: unknown[], inspect?: (url: string, options: RequestInit, index: number) => void) {
  let calls = 0
  const fetcher = (async (url: string, options: RequestInit) => {
    const index = calls++
    assert.ok(index < values.length, "request count is bounded")
    inspect?.(url, options, index)
    return new Response(JSON.stringify(values[index]))
  }) as unknown as Fetch
  return { fetcher, calls: () => calls }
}

test("decodes Python shortuuid vectors and UUID range boundaries", () => {
  // Generated with installed Python shortuuid.encode(UUID(value)), default alphabet.
  for (const [encoded, uuid] of [
    ["2222222222222222222222", "00000000-0000-0000-0000-000000000000"],
    ["546fMQ5WFDaZtG7F87gYX8", userId], ["86BKgn8zPuCmdySZTr5nk8", albumId],
    ["oZEq7ovRbLq6UnGMPwc8B5", "ffffffff-ffff-ffff-ffff-ffffffffffff"],
  ]) assert.equal(gumnutIdToUuid(`album_${encoded}`, "album"), uuid)
})
test("rejects wrong prefixes, malformed, short, long, and overflowing IDs", () => {
  for (const value of [null, 1, "", upstreamUserId, "album_0".padEnd(28, "2"),
    "album_222222222222222222222", "album_22222222222222222222222",
    "album_zzzzzzzzzzzzzzzzzzzzzz", "album_222222222222222222222_",
  ]) assert.throws(() => gumnutIdToUuid(value, "album"))
})
test("verifies the fixed curated fixture via five bounded GETs and returns only sanitized settings", async () => {
  const expected = [
    "https://api.gumnut.ai/api/users/me",
    "https://immich-adapter-docker.onrender.com/api/users/me",
    "https://api.gumnut.ai/api/libraries",
    `https://api.gumnut.ai/api/albums?library_id=${libraryId}&name=Architecture&limit=2`,
    `https://immich-adapter-docker.onrender.com/api/albums/${albumId}`,
  ]
  const runner = mock(payloads(), (url, options, index) => {
    assert.equal(url, expected[index])
    assert.equal(options.method, "GET")
    assert.equal(options.redirect, "manual")
    assert.deepEqual(options.headers, index === 1 || index === 4
      ? { "x-api-key": secret } : { Authorization: `Bearer ${secret}` })
    assert.equal(options.body, undefined)
    assert.ok(options.signal instanceof AbortSignal)
  })
  const result = await discoverFixture({ ...env, GUMNUT_API_BASE_URL: "https://example.com", IMMICH_SMOKE_ORIGIN: "https://example.com" }, runner.fetcher)
  assert.deepEqual(result, { IMMICH_SMOKE_USER_ID: userId, IMMICH_SMOKE_ALBUM_ID: albumId,
    IMMICH_SMOKE_ALBUM_NAME: "Architecture", IMMICH_SMOKE_ASSET_COUNT: "91" })
  assert.equal(runner.calls(), 5)
  assert.equal(JSON.stringify(result).includes(secret), false)
})
test("accepts the explicitly selected sole library", async () => {
  const values = payloads()
  ;(values[0] as Record<string, unknown>).immich_library_id = libraryId
  await discoverFixture(env, mock(values).fetcher)
})

const failures: [string, (values: any[]) => void][] = [
  ["wrong Gumnut account", v => { v[0].email = "user@example.com" }],
  ["inactive Gumnut account", v => { v[0].is_active = false }],
  ["wrong adapter account", v => { v[1].email = "user@example.com" }],
  ["inconsistent adapter ID", v => { v[1].id = albumId }],
  ["inactive adapter", v => { v[1].status = "deleted" }],
  ["absent preferred library field", v => { delete v[0].immich_library_id }],
  ["wrong selected library", v => { v[0].immich_library_id = "lib_other" }],
  ["missing library", v => { v[2] = [] }],
  ["multiple libraries", v => { v[2].push(v[2][0]) }],
  ["unowned library", v => { v[2][0].user_id = "intuser_other" }],
  ["wrong owner", v => { v[2][0].owner.id = "intuser_other" }],
  ["wrong owner_id when supplied", v => { v[2][0].owner_id = "intuser_other" }],
  ["viewer library", v => { v[2][0].role = "viewer" }],
  ["malformed library ID", v => { v[2][0].id = "lib_unsafe?" }],
  ["missing album", v => { v[3].data = [] }],
  ["ambiguous album", v => { v[3].data.push(v[3].data[0]) }],
  ["more album pages", v => { v[3].has_more = true }],
  ["wrong exact name", v => { v[3].data[0].name = "Architecture extra" }],
  ["empty album", v => { v[3].data[0].asset_count = 0 }],
  ["fractional count", v => { v[3].data[0].asset_count = 1.5 }],
  ["unsafe count", v => { v[3].data[0].asset_count = Number.MAX_SAFE_INTEGER + 1 }],
  ["malformed album ID", v => { v[3].data[0].id = "album_invalid" }],
  ["adapter ID mismatch", v => { v[4].id = userId }],
  ["adapter count mismatch", v => { v[4].assetCount = 90 }],
  ["adapter owner mismatch", v => { v[4].albumUsers[0].user = { ...v[1], id: albumId } }],
]
for (const [name, corrupt] of failures) test(`fails closed: ${name}`, async () => {
  const values = payloads()
  corrupt(values)
  await assert.rejects(discoverFixture(env, mock(values).fetcher))
})

test("requires a credential before any request", async () => {
  const runner = mock([])
  await assert.rejects(discoverFixture({}, runner.fetcher))
  assert.equal(runner.calls(), 0)
})
test("inspection CLI reports a sanitized missing-credential diagnostic", () => {
  const result = spawnSync(process.execPath, ["scripts/inspect-fixture.ts"], {
    cwd: new URL("..", import.meta.url), encoding: "utf8", timeout: 10_000,
    env: { ...process.env, IMMICH_SMOKE_API_KEY: "", GITHUB_STEP_SUMMARY: "" },
  })
  assert.equal(result.status, 1)
  assert.equal(result.stdout, "")
  assert.equal(result.stderr.trim(), "IMMICH_SMOKE_API_KEY: fixture verification failed")
})
test("network exceptions cannot disclose secrets or payloads", async () => {
  await assert.rejects(discoverFixture(env, (async () => { throw new Error(secret) }) as unknown as Fetch), error => {
    assert.ok(error instanceof Error)
    assert.equal(error.message, "Gumnut user: request failed")
    assert.equal(error.stack?.includes(secret), false)
    assert.equal("cause" in error, false)
    return true
  })
})
for (const status of [301, 302, 307, 401, 500]) test(`rejects HTTP ${status} without following redirects or retrying`, async () => {
  let calls = 0
  await assert.rejects(discoverFixture(env, (async () => {
    calls++
    return new Response(secret, { status, headers: { location: "https://example.com" } })
  }) as unknown as Fetch))
  assert.equal(calls, 1)
})
test("bounds responses using both Content-Length and actual streamed bytes", async () => {
  for (const response of [
    new Response("{}", { headers: { "content-length": String(1024 * 1024 + 1) } }),
    new Response("x".repeat(1024 * 1024 + 1)),
    new Response("x".repeat(1024 * 1024 + 1), { headers: { "content-length": "2" } }),
    new Response(secret),
  ]) await assert.rejects(discoverFixture(env, (async () => response) as unknown as Fetch))
})
test("one deadline covers both headers and a stalled body and aborts requests", async () => {
  for (const bodyStall of [false, true]) {
    let signal: AbortSignal | null | undefined
    const fetcher = (async (_url: string, options: RequestInit) => {
      signal = options.signal
      if (bodyStall) return new Response(new ReadableStream({ start() {} }))
      return await new Promise<Response>(() => {})
    }) as unknown as Fetch
    await assert.rejects(discoverFixture(env, fetcher, 10), /Gumnut user: timeout/)
    assert.equal(signal?.aborted, true)
  }
})
