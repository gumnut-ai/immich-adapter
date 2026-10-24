# Daily dependency maintenance

Common dependency-maintenance instructions are imported from an immutable
`automation` revision and embedded in the compiled workflow. The local
`DAEMON.md` retains repository scope, limits, checks and exceptions.

The reviewed `.github/workflows/dependency-maintenance.md` source and compiled
`.lock.yml` schedule one activation daily at 15:00 UTC. After merge to the default
branch the overlay is enabled without a repository-variable gate; disable the
workflow in Actions to stop admissions. Dispatch accepts only the default branch
and no prompt context. Compilation does not prove hosted execution or App access.

The dispatch-revision `.agents/daemons/dependency-maintenance/DAEMON.md` owns
selection, deduplication, compatibility, one-code-proposal and two-open-role-PR
caps, three new commits, and up to five readiness-only handoffs. The role first
inspects existing dependency PRs and feedback, then uncovered Python, script,
smoke-test Bun, codegen and compatible Immich pins. Existing Renovate and Dependabot automation
remains authoritative for its configured coverage and release-age policy.
Major migrations, generated API rewrites and auth changes require human work.
The legacy dependency-snapshot collector stays disabled and is not an input.

The agent uses the repository Actions token through read-only GitHub tools
with native `allowed-repos: all`: its current repository and public upstream
release evidence are accessible, while other private repositories remain outside
this token's authority. Native sink-visibility checks remain enabled.
Native safe outputs publish new PRs or append repairs to eligible existing
same-repository bot PRs. The SHA-pinned `gumnut-ai/automation` dependency-delivery
action runs only in the isolated publisher, prepares trusted controls outside
the candidate checkout, constrains output targets and reads back human handoffs.
It exposes `DEPENDENCY_MAINTENANCE_TRUSTED` for the native handoff handler.
Workflows and active automation controls remain native protected files; a
blocked update is a human delivery blocker. CI and independent human review
remain required. The overlay never approves, merges, replaces or closes PRs.
A readiness handoff requires the unchanged exact head to have passing required
checks, no actionable findings and known mergeability with main.

Provision `CODEX_API_KEY` for isolated inference and detection and the
`gumbot-publisher` environment with `GUMBOT_CLIENT_ID` and `GUMBOT_PRIVATE_KEY`.
The installed App needs Contents and Pull requests write plus Issues read.
Keep publisher credentials out of the agent; no production service credentials
are used. Native limits bound individual execution and rolling admission;
manual dispatch skips the daily admission check. No report or cost-measurement
pipeline is created.

The smoke-test Bun pin is installed by setup-bun and exposed read-only from
`/home/runner/.bun/bin/bun` on the selected Linux runner into AWF. Verify its
version inside the sandbox after deployment. Compatible smoke-test manifests and
locks may be proposed; production smoke execution remains operator-owned.

Compile with gh-aw v0.89.21:

```bash
node /path/to/automation/dependency-maintenance/compile.cjs /path/to/gh-aw
uv run pytest tests/test_dependency_maintenance_workflow.py tests/test_librarian_workflow.py
uv run --no-config --locked --script scripts/lint_docs.py --base origin/main
git diff --check
```

Use the reviewed `automation` checkout pinned by workflow CI, running from this
repository root. The shared compiler owns dependency detector-image,
admission-permission and publisher-permission corrections; the local maintainer
compiler handles only the other daemon roles. Contract tests run in the existing pytest
suite; no separate CI dispatcher or duplicate quality gate is needed.

Local compile helpers load `native-toolchain` from the same immutable
`.shared-automation` checkout as workflow CI. Common detector, action-annotation
and publisher-permission compatibility is owned by
[`automation`](https://github.com/gumnut-ai/automation/tree/ec2bba2086ad77a4db2a13aa17ac97b3b2149463/native-toolchain);
Repository-specific policy corrections remain local.
