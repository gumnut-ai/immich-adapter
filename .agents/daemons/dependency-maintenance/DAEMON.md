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

Publish code proposals for ordinary CI, and revisit them on a later daily run.
Before a readiness-only handoff, inspect the exact current head: required checks
passed, no unresolved actionable findings or known relevant failures, and known
mergeability with main. Pending/missing checks or unknown mergeability mean
waiting, never ready. Missing human approval is expected, not a reason to approve
automatically. Do not hand off a PR whose code you changed in this activation.

Select an eligible human using the shared daemon contribution-history policy.
Inspect current assignments, requested reviewers and completed reviews before
calling `handoff_dependency_pr`. Existing eligible assignments and pending
requests satisfy their respective actions. A completed eligible review on the
current head also satisfies review delivery; older-head reviews do not. Treat
unresolved changes requested as feedback to address before any readiness handoff.
Call with the PR number, observed full head SHA, eligible login and only the
missing `action`: `assign` or `request_review`. If both are missing, issue one
call for each; if both are satisfied, issue neither. Preserve existing humans
and emit no duplicate action for the same PR. The isolated operation executes
the selected action once and reads it back; a failure is an incomplete handoff.
Emit at most five readiness-only handoffs per activation.

Use native no-op only after completed discovery finds no useful code work or
handoff. Missing evidence/tools, denied resources and failed checks are incomplete
work; use native `missing_data` or `missing_tool`, not no-op. Keep verified findings and capacity deferrals visible in native outcomes.
This daemon creates no separate report and performs no cost measurement.
