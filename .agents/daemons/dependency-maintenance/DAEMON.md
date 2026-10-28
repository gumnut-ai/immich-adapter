---
id: dependency-maintenance
purpose: Advances mechanical dependency PRs and proposes useful uncovered updates.
schedule: "0 15 * * *"
---

Apply the shared dependency-maintenance instructions embedded by the owning
workflow, together with the repository policy below.

## Scope and priorities

Own one bounded dependency-maintenance activation in the adapter. Read the
[agent-workflow guidance](../../../docs/references/agent-workflows.md),
[development-tool guidance](../../../docs/references/development-tools.md) and
owning-project instructions. Follow the root pyproject.toml resolver cooldowns, exemptions,
compatibility bounds and generated-artifact procedures. Keep judgment with the
agent and validate proposals through documented checks, normal CI and independent
review; do not invent a second collector, classifier or CI dispatcher.

Start with PRs created or updated in the last 30 days, and inspect older open PRs
so unresolved work cannot age out. Paginate relevant reads. Use recent merged or
closed PRs for overlap and failure context, never to reopen rejected proposals.

Compare tracked manifests, locks, runtime/tool pins and script/build inputs with
actual bot coverage. Consider compatible transitive updates, constrained ranges,
PEP 723 script locks and coordinated pins. An absent PR can mean a cooldown,
schedule, update branch or deliberate exclusion; establish a real gap before
proposing competing work. Read available advisory alerts and use native audits
where graph coverage is incomplete, including Python audits. Record actual
sources, roots, limitations and failed checks in the native activation outcome;
never infer universal coverage from an empty or unsupported source.

An existing PR covers an advisory only when its actual candidate versions fix
every affected occurrence. Inspect versions and release dates, including
incidental lock changes and script entries without resolver age guards. An issue,
PR title or package-name match alone does not establish a fix.

## Repair and proposal boundaries

Make at most one topical code proposal: repair one existing PR or open/update
one role PR. A coordinated runtime bump may span its related roots and artifacts;
otherwise batch by lock root or project. Use at most three new commits, one new
PR per activation and two open role-created PRs. Bot PRs do not count against the
new-PR cap or prevent repairing existing work. New branches use
`dependency-maintenance/<unused-topic>`. Record source revision, old/candidate
versions, useful change, related PR/advisory evidence and checks in the PR.
These are agent-owned limits: paginate and count current role PRs before new
work, choose one proposal across both native code tools, and record deferrals.

Never weaken tests, add retries or suppressions just to pass CI, relax policy or add
cooldown exceptions. Preserve dependency cooldowns and the first-party exemption. Inspect Python
manifests, locks, PEP 723 script metadata/locks and codegen tool pins. Read the
[upstream compatibility reference](../../../docs/references/upstream-compatibility.md)
and [Immich upgrade guide](../../../docs/guides/upgrading-immich-version.md)
before proposing compatible coordinated codegen or Immich pin updates. Immich
pins remain a compatibility decision: only changes preserving the existing
contract belong here; no major retarget, generated API rewrite or auth change.

## Human handoff and outcomes

Follow [Human review for daemon-created PRs](../AGENTS.md#human-review-for-daemon-created-prs)
for later clean-head handoff. A clean dependency PR needs both an eligible human
assignment and review delivery; emit only missing actions.

Follow the shared outcome rules: noop requires completed no-work discovery;
blocked investigation or selected-proposal checks require `report_incomplete`
when no validated delivery can proceed. Keep unrelated discovery gaps and
capacity deferrals in the proposal body and native activation context without
combining successful delivery with an incomplete signal.
This daemon creates no separate report and performs no cost measurement.
