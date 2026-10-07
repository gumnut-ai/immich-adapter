# Codebase cleanup and dependency coverage

`maintainer.md` runs daily codebase cleanup when `MAINTAINER_ENABLED=true`.
Dependabot owns dependency-update PRs. Cleanup does not poll dependency alerts or
require an alert-reader credential or indexing receipt. The trusted candidate
validator rejects dependency manifests/locks and sensitive policy/test/config
changes. Full affected-app checks, isolated publishing and human handoff remain
required; a no-op must explain why no material cleanup was justified.

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
result. Security alerts and update proposals are handled by GitHub Dependabot;
cleanup does not provide an advisory-response SLA. Complex upgrade repairs must
be scoped separately and remain subject to repository dependency policy.

Primary contracts:
- https://docs.github.com/en/rest/dependency-graph/dependency-submission
- https://docs.github.com/en/graphql/reference/dependency-graph
- https://docs.astral.sh/uv/concepts/projects/layout/#the-lockfile
- https://bun.sh/docs/pm/lockfile
- https://github.com/oven-sh/bun/blob/main/packages/bun-types/bun.d.ts
- https://packaging.python.org/en/latest/specifications/version-specifiers/#local-version-identifiers
