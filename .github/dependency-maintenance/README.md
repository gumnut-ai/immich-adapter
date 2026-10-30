# Daily dependency maintenance

`.github/workflows/dependency-maintenance.md` is the source for the checked-in
`dependency-maintenance.lock.yml`. Compile it directly with unmodified GH-AW v0.89.21;
no generated YAML corrections, runtime patches or delivery adapters are used.
Shared role and existing-PR repair instructions are embedded from immutable imports. Local daemon policy owns
scope, investigation, checks, caps, deduplication and human selection.

The daily schedule is 15:00 UTC. There is no repository-variable gate. Dispatch
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
upload Git LFS objects; defer proposals requiring new uploads. Dependency repairs
append to eligible bot branches directly, with stacked fallback disabled. Agents
inspect native reconciliation or refusal and preserve human work; uncertain writes
require fresh readback before retrying.

Human handoff uses native numeric assignment and review targets on a later
activation, after exact-head checks pass, actionable findings are resolved and
mergeability with main is known. Never hand off code changed in the same run.
Existing eligible assignments, pending requests and current-head reviews satisfy
their actions. No daemon approves, merges, creates a report or measures cost.
Native budgets bound execution and rolling admission; manual dispatch bypasses
the daily admission check. Preserve completed check evidence and distinguish
publication, readiness, review, merge and deployment.

```bash
gh-aw compile dependency-maintenance --action-tag v0.89.21 --no-check-update
node --test .github/maintainer/native-policy.test.cjs
uv run --no-config --locked --script scripts/lint_docs.py --base origin/main
git diff --check
```

Workflow CI regenerates all three maintenance lockfiles directly and compares
them byte for byte. The separate PR reviewer retains its existing compatibility
code until its own migration; its helpers are not part of maintenance publication.
