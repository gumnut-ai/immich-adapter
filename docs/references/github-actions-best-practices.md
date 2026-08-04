---
title: GitHub Actions Best Practices
last-updated: 2026-07-30
---

# GitHub Actions Best Practices

Security and maintenance rules for `.github/workflows/*.yml` in this repository. A workflow executes third-party code inside the repository trust boundary with whatever token, secrets, and write scope it receives, so workflow changes get the same review rigor as application code.

## Pin Actions Deliberately

Pin every third-party action—anything outside `actions/*`—to a full 40-character commit SHA with a trailing version comment:

```yaml
- uses: owner/action@<40-character-commit-sha> # vX.Y.Z
```

Tags and branches are mutable. The comment keeps the intended release legible and lets dependency automation update the SHA and version together.

Use version tags for GitHub-owned `actions/*` actions. The checked-in `.github/zizmor.yml` uses `ref-pin` for this exemption, which mechanically permits tags or branches; the written convention is narrower, so reject branch refs in review. When bumping any action, update its version comment in the same edit.

## Declare Token Permissions

Set `permissions:` explicitly at workflow level. The usual default is:

```yaml
permissions:
  contents: read
```

Add only scopes the workflow needs. Use job-level permissions only when one job needs a wider scope than the rest, and use `permissions: {}` when no token access is required.

## Keep Untrusted Code Out of Privileged Contexts

Use `pull_request` for ordinary CI. It gives forked pull requests a read-only token and no secrets.

Avoid `pull_request_target`. If a workflow genuinely needs to label or comment on a forked PR, prefer a deferred `workflow_run` job that reads metadata without checking out the contributor's code. A metadata-only `pull_request_target` job is acceptable only when it:

- never checks out the PR head;
- never runs PR-controlled code or installs PR-controlled dependencies;
- never restores caches populated by fork PRs.

Treat adding checkout or dependency execution to such a workflow as a security regression.

## Gate Comment and Issue Triggers

`issue_comment` and `issues` can be triggered by untrusted users. Jobs using them must:

- gate on a trusted author association and a structured command;
- avoid publishing-capable tokens;
- avoid executing or shell-interpolating the comment body.

A body substring check by itself is not authorization.

## Keep Event Data Out of Shell Source

GitHub expressions expand before the shell parses `run:`. Never interpolate PR titles, branch names, commit messages, issue bodies, comments, or workflow inputs directly into shell source.

```yaml
# unsafe
- run: echo "${{ github.event.pull_request.title }}"

# safe
- env:
    PR_TITLE: ${{ github.event.pull_request.title }}
  run: echo "$PR_TITLE"
```

GitHub expression context such as an `if:` condition is evaluated by Actions and is not shell interpolation.

## Disable Checkout Credentials

Set `persist-credentials: false` on `actions/checkout` unless the job intentionally pushes commits or tags:

```yaml
- uses: actions/checkout@<current-major>
  with:
    persist-credentials: false
```

If a job must push, grant the narrow permission explicitly and document why credentials are retained.

## Run zizmor

`.github/workflows/zizmor.yml` audits workflow changes for unpinned actions, excessive permissions, template injection, dangerous triggers, and cache poisoning. `.github/zizmor.yml` owns the pin policy.

Those checked-in files are the source of truth for the current action version, triggers, severity threshold, and SARIF/annotation mode. Do not copy those tunable values into prose. The workflow must run when either `.github/workflows/**` or `.github/zizmor.yml` changes.

Prefer fixing a finding. Suppress one only with a narrow `# zizmor: ignore[rule]` annotation and an adjacent justification.

## Review Checklist

- [ ] Workflow-level `permissions:` is explicit and minimal.
- [ ] Third-party actions use a full SHA plus version comment.
- [ ] `actions/checkout` disables persisted credentials unless the job pushes.
- [ ] CI uses `pull_request`, not a privileged trigger that executes PR code.
- [ ] Comment/issue triggers strongly authorize the caller.
- [ ] Event data reaches shell through `env:`, not direct interpolation.
- [ ] Cache use does not cross from untrusted to privileged execution.
- [ ] The local zizmor workflow and configuration cover this change.

## References

- [GitHub Actions security hardening](https://docs.github.com/en/actions/security-guides/security-hardening-for-github-actions)
- [zizmor documentation](https://docs.zizmor.sh)
- [CISA: tj-actions/changed-files compromise](https://www.cisa.gov/news-events/alerts/2025/03/18/supply-chain-compromise-third-party-tj-actionschanged-files)
