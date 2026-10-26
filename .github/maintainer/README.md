# Codebase cleanup and dependency coverage

The pinned compiler bypasses its action cache for some generated steps. This
wrapper preserves reviewed artifact and generated Node action pins while leaving
the source-owned pre-agent Node step alone. Workflow CI checks all three daemon
lockfiles regenerate without changes.

Common maintainer and librarian instructions are imported from an immutable
`automation` revision and embedded during compilation. Local daemon policies
retain scope, limits, required checks and exceptions. The pinned shared
`actions/daemon-delivery` adapter owns native reviewer target resolution and
account exclusions; repository delivery readback remains local. It checks the first native created-PR
receipt only; native status covers publication processing, not aggregate reviewer
or CI readiness.

`maintainer.md` runs daily codebase maintenance when `MAINTAINER_ENABLED=true`.
Routine dependency scans and security-alert polling are no longer agent goals.
Agents can still make justified dependency changes as part of useful maintenance,
following repository dependency policy and verification guidance. Existing
configuration protection, isolated publishing and human handoff remain intact.
Cleanup needs neither an alert-reader credential nor an indexing receipt.

The workflow prepares pinned tools and preserves trusted candidate guards before
inference. The agent investigates useful changes and runs checks under repository
guidance, including required local checks. Trusted host validation enforces basic
scope, configuration and commit identity before isolated publication. Ordinary test
refactors and coverage additions are eligible; test-runner controls, automation,
policy and migration paths remain protected. The path guard does not prove test
semantics. It identifies controls by their actual paths or runner-discovered
basenames; config-named tests and YAML fixtures remain eligible. Root `config/`
runtime settings, runner configuration, dependency automation, dot-folder
automation and credentials still require escalation. Detection and independent
review must still reject deleted, skipped, xfail or weakened tests used to make
checks pass. Ordinary PR CI runs the standard suites and independent review
assesses quality. There is no
second full-suite sandbox before inference or publication. Check evidence must
distinguish completed local checks from pending PR CI. A noop means completed
discovery found no worthwhile candidate; blocked or unfinished work emits
`report_incomplete`, which delivery verification rejects.

The dependency submission workflow below is separately gated and is not needed
for cleanup activation. Enabling Dependabot settings does not establish complete
locked-dependency coverage or activate this submission workflow.

## Optional locked dependency indexing

`maintainer-dependencies.yml` is an inference-free, default-branch-only GitHub
Dependency Submission API publisher. It remains disabled unless
`MAINTAINER_DEPENDENCIES_ENABLED=true`. The separate native agent gate is
`MAINTAINER_ENABLED`; neither gate is an activation instruction.

The reviewed lock parser reads Git objects at the exact source SHA, never
installs dependencies or runs package scripts, and discovers every committed
`uv.lock`, `*.py.lock` (uv script lock), and `bun.lock`. It includes every locked
registry version across development, optional, and platform resolutions. Exact
npm name/version identities are deduplicated per manifest, including aliases.
Repository project/workspace entries are recorded as explicit exclusions.
Each manifest must have its own lock or matching declared and locked workspace membership.
Unsupported lock formats, VCS/direct/local dependencies, custom/private
registries, and uncovered package manifests fail closed rather than disappear.
Bun text formats 1/2 require the reviewed Bun 1.4.2 native JSONC parser. uv format
1/revision <=3 uses Python's standard TOML parser.

Public PyTorch CPU registry packages retain exact `+cpu` identities
and additionally submit the upstream public version for conservative advisory
matching. These builds may contain different code; CPU-only vulnerabilities
outside GitHub's npm/PyPI advisory database are not covered. Local repository
code, OS packages, container images, private/VCS packages, uncommitted or
unlocked dependencies, and vendor advisories outside that database need separate
security ownership. A public-registry lock entry cannot establish whether a package is private; indexing its identity does not prove advisory availability. The parser blocks newly unsupported dependency sources.

GitHub's API requires Contents write. Only the indexing job uses the existing
`gumbot-publisher` environment and a repository-only, Contents-write App token.
The agent receives neither that token nor the App private key. Dependency graph
readback uses a separate native read token. Cleanup receives no alert-reader token.

Before activation, the rollout owner must obtain authorization for dependency
submission and any required repository security setting, provision these gates
and credentials, and enable Dependabot alerts/dependency graph where necessary.
No workflow enables security settings. First run the indexing workflow on merged
main and retain its exact SHA, snapshot acceptance ID, per-manifest lock hashes,
counts, exclusions/limitations, and complete paginated graph readback artifact.
A POST acceptance is insufficient: a missing manifest, missing exact package
URL/version, graph truncation, mismatched inventory, or advanced branch fails.
Indexing runs on main pushes, every six hours, and manual dispatch when its gate
is enabled. Keep failed coverage visible rather than treating it as a no-alert
result. Dependabot settings/configuration determine its alert and update coverage.
This maintenance workflow does not provide an advisory-response SLA.

Primary contracts:
- https://docs.github.com/en/rest/dependency-graph/dependency-submission
- https://docs.github.com/en/graphql/reference/dependency-graph
- https://docs.astral.sh/uv/concepts/projects/layout/#the-lockfile
- https://bun.sh/docs/pm/lockfile
- https://github.com/oven-sh/bun/blob/main/packages/bun-types/bun.d.ts
- https://packaging.python.org/en/latest/specifications/version-specifiers/#local-version-identifiers

Local compile helpers load `native-toolchain` from the same immutable
`.shared-automation` checkout as workflow CI. Common detector, action-annotation
and publisher-permission compatibility is owned by
[`automation`](https://github.com/gumnut-ai/automation/tree/6e7e2d021e2a394e9e7709cedf2455b6ee8bd44c/native-toolchain);
Repository-specific policy corrections remain local.
