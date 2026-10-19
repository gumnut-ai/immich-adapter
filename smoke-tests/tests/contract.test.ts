import { test } from "node:test"
import assert from "node:assert/strict"
import { fixtureConfig, runtimeConfig, validateAlbum, validateUser } from "../checks/lib/contract"
import { runContract, type Fetch } from "../checks/lib/run"
import { scheduleActive, slackChannelId } from "../checks/lib/settings"

const env = {
  IMMICH_SMOKE_API_KEY: "example-secret-never-log",
  IMMICH_SMOKE_USER_ID: "11111111-1111-4111-8111-111111111111",
  IMMICH_SMOKE_ALBUM_ID: "22222222-2222-4222-8222-222222222222",
  IMMICH_SMOKE_ALBUM_NAME: "Example smoke album",
  IMMICH_SMOKE_ASSET_COUNT: "3",
}
const otherId = "33333333-3333-4333-8333-333333333333"
const date = "2026-01-01T00:00:00Z"
const user = {
  id: env.IMMICH_SMOKE_USER_ID, clusterGroupId: env.IMMICH_SMOKE_USER_ID,
  email: "user@example.com", name: "Example User", isAdmin: false, status: "active",
  avatarColor: "primary", profileImagePath: "", profileChangedAt: date,
  createdAt: date, updatedAt: date,
}
const album = {
  id: env.IMMICH_SMOKE_ALBUM_ID, albumName: env.IMMICH_SMOKE_ALBUM_NAME, assetCount: 3,
  shared: false, hasSharedLink: false, albumUsers: [{ role: "owner", user }],
  createdAt: date, updatedAt: date, startDate: date, endDate: date,
  lastModifiedAssetTimestamp: null, albumThumbnailAssetId: null,
}
const fixture = fixtureConfig(env)
const fetchMock = (handler: (url: string, options: RequestInit) => Promise<Response>): Fetch =>
  handler as unknown as Fetch
function response(value: unknown) { return new Response(JSON.stringify(value)) }
function sanitized(message: string) {
  return (error: unknown) => {
    assert.ok(error instanceof Error)
    assert.equal(error.message, message)
    assert.equal("cause" in error, false)
    assert.equal(JSON.stringify(error).includes(env.IMMICH_SMOKE_API_KEY), false)
    assert.equal(error.stack?.includes(env.IMMICH_SMOKE_API_KEY), false)
    return true
  }
}

test("complete fixture accepts user and seeded album with distinct UUIDs", () => {
  validateUser(user, fixture)
  validateAlbum(album, fixture)
})
for (const [key, value] of Object.entries({
  id: otherId, clusterGroupId: otherId, isAdmin: true, status: "deleted", email: null,
  name: [], createdAt: "yesterday", updatedAt: null, profileChangedAt: "invalid",
})) {
  test(`rejects user.${key} corruption`, () => {
    assert.throws(() => validateUser({ ...user, [key]: value }, fixture), new RegExp(`user\\.${key}: contract mismatch`))
  })
}
for (const [key, value] of Object.entries({
  id: otherId, albumName: "Wrong album", assetCount: 0, shared: true, hasSharedLink: true,
  albumUsers: [], createdAt: false, updatedAt: "invalid", startDate: "invalid",
  endDate: 42, lastModifiedAssetTimestamp: "invalid", albumThumbnailAssetId: "invalid",
})) {
  test(`rejects album.${key} corruption`, () => {
    assert.throws(() => validateAlbum({ ...album, [key]: value }, fixture))
  })
}
test("rejects count drift, owner identity, owner role, duplicate membership and malformed objects", () => {
  for (const value of [
    { ...album, assetCount: 4 },
    { ...album, albumUsers: [{ role: "owner", user: { ...user, id: otherId } }] },
    { ...album, albumUsers: [{ role: "editor", user }] },
    { ...album, albumUsers: [album.albumUsers[0], album.albumUsers[0]] },
    { ...album, albumUsers: [null] }, null, [], "private payload",
  ]) assert.throws(() => validateAlbum(value, fixture))
})
test("rejects empty and malformed config before traffic", async () => {
  let calls = 0
  const fetcher = fetchMock(async () => { calls++; return response(user) })
  for (const key of Object.keys(env)) {
    for (const value of [undefined, "", " "]) {
      await assert.rejects(runContract({ ...env, [key]: value }, fetcher))
    }
  }
  for (const count of ["0", "-1", "1.1", "1e3", "01", "9007199254740992", " 3"]) {
    assert.throws(() => runtimeConfig({ ...env, IMMICH_SMOKE_ASSET_COUNT: count }))
  }
  for (const key of ["IMMICH_SMOKE_USER_ID", "IMMICH_SMOKE_ALBUM_ID"]) {
    await assert.rejects(runContract({ ...env, [key]: "invalid" }, fetcher))
  }
  assert.equal(calls, 0)
})
test("only two sequential GET requests use fixed origin, API key and manual redirects", async () => {
  const urls: string[] = []
  await runContract(env, fetchMock(async (url, options) => {
    urls.push(url)
    assert.equal(options.method, "GET")
    assert.equal(options.redirect, "manual")
    assert.deepEqual(options.headers, { "x-api-key": env.IMMICH_SMOKE_API_KEY })
    assert.equal("signal" in options, false)
    return response(urls.length === 1 ? user : album)
  }))
  assert.deepEqual(urls, [
    "https://immich-adapter-docker.onrender.com/api/users/me",
    `https://immich-adapter-docker.onrender.com/api/albums/${env.IMMICH_SMOKE_ALBUM_ID}`,
  ])
})
test("first contract failure prevents the album request and contains no payload values", async () => {
  let calls = 0
  await assert.rejects(runContract(env, fetchMock(async () => {
    calls++
    return response({ ...user, name: { secret: env.IMMICH_SMOKE_API_KEY } })
  })), sanitized("user.name: contract mismatch"))
  assert.equal(calls, 1)
})
for (const status of [301, 302, 401, 403, 404, 429, 500]) {
  test(`HTTP ${status} fails without body/header diagnostics or retry`, async () => {
    let calls = 0
    await assert.rejects(runContract(env, fetchMock(async () => {
      calls++
      return new Response(env.IMMICH_SMOKE_API_KEY, { status, headers: { location: "https://example.com/private" } })
    })), sanitized(`user: HTTP status ${status}`))
    assert.equal(calls, 1)
  })
}
test("network and body exceptions and invalid JSON discard original diagnostics", async () => {
  const sensitive = new Error(`${env.IMMICH_SMOKE_API_KEY} user@example.com`)
  for (const [fetcher, message] of [
    [fetchMock(async () => { throw sensitive }), "user: transport failure"],
    [fetchMock(async () => ({ status: 200, text: async () => { throw sensitive } }) as unknown as Response), "user: body read failure"],
    [fetchMock(async () => new Response(env.IMMICH_SMOKE_API_KEY)), "user: invalid JSON"],
  ] as const) await assert.rejects(runContract(env, fetcher), sanitized(message))
})
test("deadline bounds stalled headers and bodies without transport cancellation", async () => {
  for (const fetcher of [
    fetchMock(async () => new Promise<Response>(() => {})),
    fetchMock(async () => ({ status: 200, text: async () => new Promise<string>(() => {}) }) as Response),
  ]) {
    const started = Date.now()
    await assert.rejects(runContract(env, fetcher, 10), sanitized("user: timeout"))
    assert.ok(Date.now() - started < 1000)
  }
})
test("a partially read body shares the request deadline", async () => {
  await assert.rejects(runContract(env, fetchMock(async () => {
    await new Promise(resolve => setTimeout(resolve, 15))
    return { status: 200, text: async () => new Promise<string>(() => {}) } as Response
  }), 20), { message: "user: timeout" })
})
test("Multistep bridge works without abort globals and only exposes response text", async () => {
  const descriptors = ["AbortController", "AbortSignal"].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)] as const)
  try {
    for (const [key] of descriptors) Reflect.deleteProperty(globalThis, key)
    let calls = 0
    const bridgedResponse = (value: unknown) => ({ status: 200, text: async () => JSON.stringify(value) }) as Response
    await runContract(env, fetchMock(async (_url, options) => {
      calls++
      assert.equal("signal" in options, false)
      return bridgedResponse(calls === 1 ? user : album)
    }))
    assert.equal(calls, 2)
    for (const stallBody of [false, true]) {
      calls = 0
      await assert.rejects(runContract(env, fetchMock(async (_url, options) => {
        calls++
        assert.equal("signal" in options, false)
        if (!stallBody) return new Promise<Response>(() => {})
        return { status: 200, text: async () => new Promise<string>(() => {}) } as Response
      }), 10), sanitized("user: timeout"))
      assert.equal(calls, 1)
    }
  } finally {
    for (const [key, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
    }
  }
})
test("unexpected bridged status values and getter failures cannot enter diagnostics", async () => {
  for (const value of [env.IMMICH_SMOKE_API_KEY, NaN, Infinity, null, {}, 200.5, 600]) {
    await assert.rejects(runContract(env, fetchMock(async () => ({ status: value }) as Response)), sanitized("user: invalid response status"))
  }
  await assert.rejects(runContract(env, fetchMock(async () => ({
    get status() { throw new Error(env.IMMICH_SMOKE_API_KEY) },
  }) as unknown as Response)), sanitized("user: invalid response status"))
})
test("schedule defaults off, activation is exact, and Slack channel is validated", () => {
  assert.equal(scheduleActive({}), false)
  assert.equal(scheduleActive({ SMOKE_SCHEDULE_ACTIVE: "false" }), false)
  assert.equal(scheduleActive({ SMOKE_SCHEDULE_ACTIVE: "true" }), true)
  for (const value of ["", "TRUE", "1", " false "]) assert.throws(() => scheduleActive({ SMOKE_SCHEDULE_ACTIVE: value }))
  assert.equal(slackChannelId({ CHECKLY_SLACK_CHANNEL_ID: "123" }), 123)
  for (const value of [undefined, "", "0", "1.5", "-1", "9007199254740992"]) {
    assert.throws(() => slackChannelId({ CHECKLY_SLACK_CHANNEL_ID: value }))
  }
})
