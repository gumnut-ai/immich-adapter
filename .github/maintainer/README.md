# Codebase cleanup and dependency coverage

`.github/workflows/maintainer.md` is the source for the checked-in
`maintainer.lock.yml`. Compile it directly with unmodified GH-AW v0.89.21;
no generated YAML corrections, runtime patches or delivery adapters are used.
Shared instructions are embedded from immutable imports. Local daemon policy owns
scope, investigation, checks, caps, deduplication and human selection.

The daily schedule is 09:00 UTC. Admission requires `MAINTAINER_ENABLED=true`. Dispatch
accepts only the default branch and no prompt context. Compilation and CI do not
establish hosted execution, credential access or activation. Changes on an
unmerged branch do not change the active default-branch workflow.

The agent has read-only GitHub access. Native safe outputs publish through the
separate `gumbot-publisher` environment using the existing `GUMBOT_CLIENT_ID`
variable and `GUMBOT_PRIVATE_KEY` secret for the repository-scoped App.
`CODEX_API_KEY` supports inference and detection. Publication requires a successful
agent job and native threat detection. The existing installed App permissions
include Contents, Pull requests, Issues and Workflows write, and Administration
read; native token minting requests the permissions needed by configured outputs.
The agent never receives the App key or publisher token.

Native publication creates same-repository PRs. Workflow and security-file edits
within the role's scope may be proposed for ordinary CI and human review. There is
no custom path validator or captured-head rejection. Native transport cannot
upload Git LFS objects; defer proposals requiring new uploads. This role creates
proposals rather than repairing dependency branches. Inspect uncertain publication
through GitHub readback before retrying.

Human handoff uses a native numeric review target on a later
activation, after exact-head checks pass, actionable findings are resolved and
mergeability with main is known. Never hand off code changed in the same run.
Existing eligible assignments, pending requests and current-head reviews satisfy
their actions. No daemon approves, merges, creates a report or measures cost.
Native budgets bound execution and rolling admission; manual dispatch bypasses
the daily admission check. Preserve completed check evidence and distinguish
publication, readiness, review, merge and deployment.

```bash
gh-aw compile maintainer --action-tag v0.89.21 --no-check-update
node --test .github/maintainer/native-policy.test.cjs
uv run --no-config --locked --script scripts/lint_docs.py --base origin/main
git diff --check
```

Workflow CI regenerates all three maintenance lockfiles directly and compares
them byte for byte. The separate PR reviewer retains its existing compatibility
code until its own migration; its helpers are not part of maintenance publication.

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
