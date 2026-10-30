# Scheduled documentation librarian

`.github/workflows/librarian.md` is the source for the checked-in
`librarian.lock.yml`. Compile it directly with unmodified GH-AW v0.89.21;
no generated YAML corrections, runtime patches or delivery adapters are used.
Shared instructions are embedded from immutable imports. Local daemon policy owns
scope, investigation, checks, caps, deduplication and human selection.

The daily schedule is 12:00 UTC. Admission requires `LIBRARIAN_ENABLED=true`. Dispatch
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

Native publication creates same-repository PRs and repairs existing librarian PRs.
The repair output requires the `librarian` label; agent policy additionally verifies
same-repository role ownership, scope, target branch and observed head. Repairs
append commits to the existing head with native bundle transport, preserve human
edits and pending requests, and never rewrite history or update a PR created in
the same activation. Failed direct pushes cannot open a fallback PR. The agent
prefers repairs over new overlapping work and retains blocked PR evidence for an
authorized shepherd when native tools cannot safely complete a repair.

Workflow and security-file edits within the role's scope may be proposed for
ordinary CI and human review. There is
no custom path validator or captured-head rejection. Native transport cannot
upload Git LFS objects; defer proposals requiring new uploads. Repairs remain
within the librarian's documentation scope and existing work caps. Inspect
uncertain publication through GitHub readback before retrying.

Human handoff uses a native numeric review target on a later
activation, after exact-head checks pass, actionable findings are resolved and
mergeability with main is known. Never hand off code changed in the same run.
Existing eligible assignments, pending requests and current-head reviews satisfy
their actions. No daemon approves, merges, creates a report or measures cost.
Native budgets bound execution and rolling admission; manual dispatch bypasses
the daily admission check. Preserve completed check evidence and distinguish
publication, readiness, review, merge and deployment.

```bash
gh-aw compile librarian --action-tag v0.89.21 --no-check-update
node --test .github/maintainer/native-policy.test.cjs
uv run --no-config --locked --script scripts/lint_docs.py --base origin/main
git diff --check
```

Workflow CI regenerates all three maintenance lockfiles directly and compares
them byte for byte. The separate PR reviewer retains its existing compatibility
code until its own migration; its helpers are not part of maintenance publication.
