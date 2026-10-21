# Production adapter smoke catalog

One headless Checkly Multistep check exercises the deployed adapter's
authenticated user and known-album translation paths. It sends two GETs,
opens no browser, and performs no writes. It asserts the fixture's Immich
UUIDs, album title and count, and the owner relationship between responses.
A constant health ping cannot exercise this path.

```sh
bun install --frozen-lockfile
bun run check
```

`check` runs local contract and transport tests, type checking, and a
credential-free Checkly parse/validation/bundle pass. It sends no production
requests. Use the Bun version in `.bun-version`.

The [operator guide](../docs/guides/production-smoke-testing.md) owns credential
custody, fixture selection, activation evidence, pause/resume, and incident
handling. The deployment workflow is opt-in and the schedule defaults to
inactive. Merging this catalog alone does not activate monitoring.

The production smoke workflow also has explicit `inspect`, recorded `test`,
and `deploy` operations on `main`. Use them through the operator guide to
verify and pin the safe demo fixture, then validate the hosted catalog using
injected CI credentials.
