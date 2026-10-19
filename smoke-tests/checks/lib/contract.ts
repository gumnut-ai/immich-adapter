export type Environment = Record<string, string | undefined>
export const FIXTURE_KEYS = [
  "IMMICH_SMOKE_USER_ID", "IMMICH_SMOKE_ALBUM_ID",
  "IMMICH_SMOKE_ALBUM_NAME", "IMMICH_SMOKE_ASSET_COUNT",
] as const
export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

// Failure text contains only author-controlled categories and field paths.
export class ContractError extends Error {
  constructor(field: string, category: string) {
    super(`${field}: ${category}`)
    this.name = "ContractError"
  }
}
function requireField(ok: unknown, field: string): asserts ok {
  if (!ok) throw new ContractError(field, "contract mismatch")
}
export function fixtureConfig(env: Environment) {
  for (const key of FIXTURE_KEYS) requireField(env[key]?.trim(), key)
  requireField(UUID.test(env.IMMICH_SMOKE_USER_ID!), "IMMICH_SMOKE_USER_ID")
  requireField(UUID.test(env.IMMICH_SMOKE_ALBUM_ID!), "IMMICH_SMOKE_ALBUM_ID")
  const count = env.IMMICH_SMOKE_ASSET_COUNT!
  requireField(/^[1-9][0-9]*$/.test(count) && Number.isSafeInteger(Number(count)), "IMMICH_SMOKE_ASSET_COUNT")
  return {
    userId: env.IMMICH_SMOKE_USER_ID!, albumId: env.IMMICH_SMOKE_ALBUM_ID!,
    albumName: env.IMMICH_SMOKE_ALBUM_NAME!, assetCount: Number(count),
  }
}
export function runtimeConfig(env: Environment) {
  const fixture = fixtureConfig(env)
  requireField(env.IMMICH_SMOKE_API_KEY?.trim(), "IMMICH_SMOKE_API_KEY")
  return { ...fixture, apiKey: env.IMMICH_SMOKE_API_KEY!.trim() }
}
type Fixture = ReturnType<typeof fixtureConfig>
function object(value: unknown, field: string): Record<string, unknown> {
  requireField(value !== null && typeof value === "object" && !Array.isArray(value), field)
  return value as Record<string, unknown>
}
function timestamp(value: unknown, field: string, nullable = false) {
  requireField((nullable && value === null) || (typeof value === "string" &&
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) &&
    Number.isFinite(Date.parse(value))), field)
}
function basicUser(value: unknown, id: string, path: string) {
  const user = object(value, path)
  requireField(user.id === id, `${path}.id`)
  for (const field of ["email", "name", "avatarColor", "profileImagePath"]) {
    requireField(typeof user[field] === "string", `${path}.${field}`)
  }
  timestamp(user.profileChangedAt, `${path}.profileChangedAt`)
  return user
}
export function validateUser(value: unknown, fixture: Fixture) {
  const user = basicUser(value, fixture.userId, "user")
  requireField(user.clusterGroupId === fixture.userId, "user.clusterGroupId")
  requireField(user.isAdmin === false, "user.isAdmin")
  requireField(user.status === "active", "user.status")
  timestamp(user.createdAt, "user.createdAt")
  timestamp(user.updatedAt, "user.updatedAt")
}
export function validateAlbum(value: unknown, fixture: Fixture) {
  const album = object(value, "album")
  requireField(album.id === fixture.albumId, "album.id")
  requireField(album.albumName === fixture.albumName, "album.albumName")
  requireField(album.assetCount === fixture.assetCount, "album.assetCount")
  requireField(album.shared === false, "album.shared")
  requireField(album.hasSharedLink === false, "album.hasSharedLink")
  requireField(Array.isArray(album.albumUsers) && album.albumUsers.length === 1, "album.albumUsers")
  const owner = object(album.albumUsers[0], "album.albumUsers[0]")
  requireField(owner.role === "owner", "album.albumUsers[0].role")
  basicUser(owner.user, fixture.userId, "album.albumUsers[0].user")
  timestamp(album.createdAt, "album.createdAt")
  timestamp(album.updatedAt, "album.updatedAt")
  for (const key of ["startDate", "endDate", "lastModifiedAssetTimestamp"]) timestamp(album[key], `album.${key}`, true)
  requireField(album.albumThumbnailAssetId === null || (typeof album.albumThumbnailAssetId === "string" && UUID.test(album.albumThumbnailAssetId)), "album.albumThumbnailAssetId")
}
