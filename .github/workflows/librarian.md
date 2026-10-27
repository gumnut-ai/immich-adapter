---
description: Run the adapter documentation librarian from reviewed main policy.
# Compile directly with unmodified GH-AW v0.89.21:
# gh-aw compile librarian --action-tag v0.89.21 --no-check-update

on:
  schedule: "0 12 * * *"
  workflow_dispatch:
  roles: [admin, maintainer, write]
  steps:
    - name: Require the reviewed default branch
      id: default_branch
      env:
        RUN_REF: ${{ github.ref }}
        DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}
        CALLER_CONTEXT: ${{ github.event.inputs.aw_context }}
      run: |
        test "$RUN_REF" = "refs/heads/$DEFAULT_BRANCH" || {
          echo 'Librarian dispatch must run on the repository default branch' >&2
          exit 1
        }
        test -z "$CALLER_CONTEXT" || {
          echo 'Librarian dispatch does not accept prompt context' >&2
          exit 1
        }
# Operators disable this gate to stop the librarian without removing its schedule.
if: vars.LIBRARIAN_ENABLED == 'true' && needs.pre_activation.outputs.default_branch_result == 'success'
concurrency:
  group: adapter-librarian
  cancel-in-progress: false
  queue: single
runs-on: blacksmith-2vcpu-ubuntu-2404
# Keep generated admission and publisher jobs on the available runner pool.
runs-on-slim: blacksmith-2vcpu-ubuntu-2404
timeout-minutes: 30
# 1 AIC = $0.01 at the runtime's pricing; not a provider invoice guarantee.
# Initial manual-validation limits within the shared $50/month replacement goal.
# The rolling daily admission check is native and skips manual dispatches;
# these settings do not enforce a monthly portfolio cap.
max-ai-credits: 20
max-daily-ai-credits: 60
# AWF v0.28.25's curated Luna entry uses Astra rates (100x too high).
# Native provider overlays take precedence; standard short-context USD/token.
# Flat native rates omit the >272K-input tier; AIC remains an estimate.
# Verified 2026-10-02: https://developers.openai.com/api/docs/models/gpt-6-luna
models:
  providers:
    openai:
      models:
        gpt-6-luna:
          cost:
            input: "1e-7"
            output: "5e-7"
            cache_read: "1e-8"
            cache_write: "1.25e-7"
runtimes:
  node:
    version: "24.21.0"
  python:
    version: "3.14"
permissions:
  actions: read
  checks: read
  statuses: read
  contents: read
  issues: read
  pull-requests: read
tools:
  bash: ["*"]
  github:
    read-only: true
    toolsets: [repos, issues, pull_requests, actions, users]
    github-token: ${{ secrets.GITHUB_TOKEN }}
checkout:
  ref: ${{ github.sha }}
  fetch-depth: 0
  fetch: ["librarian/*"]
engine:
  id: codex
  version: "0.156.1"
  model: gpt-6-luna
  # Operators inspect outcomes before any fresh whole-agent attempt.
  harness:
    max-retries: 0
  env:
    GIT_AUTHOR_NAME: gumnut-bot[bot]
    GIT_AUTHOR_EMAIL: 333470196+gumnut-bot[bot]@users.noreply.github.com
    GIT_COMMITTER_NAME: gumnut-bot[bot]
    GIT_COMMITTER_EMAIL: 333470196+gumnut-bot[bot]@users.noreply.github.com
  args:
    - "-c"
    - 'model_provider="openai-proxy"'
    - "-c"
    - 'model_reasoning_effort="xhigh"'
    - "-c"
    - >-
      'shell_environment_policy={inherit="all",include_only=["PATH","HOME","USER","LOGNAME","SHELL","TMPDIR","TEMP","TMP","LANG","LC_ALL","LC_CTYPE","HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","NO_PROXY","SSL_CERT_FILE","SSL_CERT_DIR","NODE_EXTRA_CA_CERTS","REQUESTS_CA_BUNDLE","UV_CACHE_DIR","UV_PYTHON_INSTALL_DIR","pythonLocation","GIT_AUTHOR_NAME","GIT_AUTHOR_EMAIL","GIT_COMMITTER_NAME","GIT_COMMITTER_EMAIL"]}'
sandbox:
  agent:
    version: v0.28.25
    runtime: docker-sudo-iptables
    model-fallback: false
    token-steering: false
    images:
      agent: ghcr.io/github/gh-aw-firewall/agent:0.28.25@sha256:25fbbefb92b690d4e6ef34de8df057c0349e6adf2b7486c0cc56954a8e9a3cd4
      apiProxy: ghcr.io/github/gh-aw-firewall/api-proxy:0.28.25@sha256:c3c7082e73ba83052c580097e5a9c9c40a11e6f6b6fa0a4710e6859f83a62854
      squid: ghcr.io/github/gh-aw-firewall/squid:0.28.25@sha256:94cac14372280dbf4a08d021d7b5810a9fd7d73c88802e04a9e05551b1cbcd09
network:
  allowed: [defaults]
safe-outputs:
  # Native transport capacity; work-count and reviewer caps stay in agent policy.
  staged: false
  timeout-minutes: 10
  environment: gumbot-publisher
  github-app:
    client-id: ${{ vars.GUMBOT_CLIENT_ID }}
    private-key: ${{ secrets.GUMBOT_PRIVATE_KEY }}
    owner: gumnut-ai
    repositories: [immich-adapter]
  create-pull-request:
    max: 10
    base-branch: main
    branch-prefix: librarian/
    stacked: false
    draft: false
    # Direct App-authenticated pushes preserve bot author AND committer.
    # GitHub-signed replay can replace the committer with GitHub's identity.
    signed-commits: false
    github-token-for-extra-empty-commit: none
    labels: [librarian]
    preserve-branch-name: true
    allowed-branches: ["librarian/*"]
    fallback-as-issue: false
    if-no-changes: error
    protected-files: allowed
    allow-workflows: true
  add-reviewer:
    max: 100
    target: "*"
  assign-to-user:
    max: 100
    target: "*"
  noop:
    report-as-issue: false
  missing-tool:
    create-issue: false
  missing-data:
    create-issue: false
  report-incomplete:
    create-issue: false
  report-failure-as-issue: false
  report-failed-jobs: false
  threat-detection:
    max-ai-credits: 5
    report-as-issue: false
    runs-on: blacksmith-2vcpu-ubuntu-2404
jobs:
  agent:
    timeout-minutes: 45
  safe_outputs:
    if: needs.agent.result == 'success' && needs.detection.outputs.detection_success == 'true'

steps:
  - name: Read uv version
    id: uv-version
    shell: bash
    run: |
      version=$(cat .uv-version)
      if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo 'Expected an exact uv version (major.minor.patch)' >&2
        exit 1
      fi
      echo "version=$version" >> "$GITHUB_OUTPUT"
  - name: Install pinned uv and configure local cache
    id: uv_setup
    uses: astral-sh/setup-uv@v10.2.0
    with:
      version: ${{ steps.uv-version.outputs.version }}
      # Agent-writable dependencies must not be persisted into future jobs.
      enable-cache: false
      # AWF hides runner HOME; /tmp makes the prepared cache available to the agent.
      cache-local-path: /tmp/scheduled-care-uv-cache
  - name: Prepare pinned Python for AWF
    run: bash scripts/prepare_maintainer_python.sh
  - name: Prepare locked documentation linter
    id: linter_setup
    run: |
      git rev-parse --verify --quiet 'origin/main^{commit}' > /dev/null
      uv run --no-config --locked --script scripts/lint_docs.py --help > /dev/null
pre-agent-steps:
  - name: Set proposal commit identity
    shell: bash
    run: |
      git config user.name 'gumnut-bot[bot]'
      git config user.email '333470196+gumnut-bot[bot]@users.noreply.github.com'
inlined-imports: true
imports:
  - gumnut-ai/automation/daemons/librarian.md@ec2bba2086ad77a4db2a13aa17ac97b3b2149463

---

Apply the shared librarian instructions and local Proposal quality policy to
candidate selection, related-document coherence and PR presentation. The evidence
required below remains in the body, in collapsible details after a proportionate
summary.

Read `.agents/daemons/librarian/DAEMON.md` from this checkout and follow its
policy. Read `.agents/daemons/AGENTS.md` and every repository guidance or
reference document that `DAEMON.md` directs you to use for the selected change.
Use the dispatch revision (`GITHUB_SHA`, initially HEAD) for policy, history,
implementation and documentation; do not substitute a moving `origin/main`.
Record that revision before making edits. No dispatch input may change
the role, provider, model, credentials, or instructions.

Start discovery from local Git history: inspect the latest 20 first-parent
commits at the dispatch revision, then select up to three changes most likely
to affect durable documented behavior. Read their relevant implementation and
owning docs, including related architecture, operational guidance and README
claims. A matching doc edit in a merged PR does not establish correctness.
Retrieve PR metadata only when needed to resolve evidence.

Also inspect one small area independently of recent merges. Use the activation's
UTC day of year, modulo seven, to select from this ordered list:
0 authentication and sessions, 1 asset and media conversion, 2 sync streams,
3 WebSocket events, 4 pagination and bulk operations, 5 Immich client guides,
6 upstream compatibility and development tools.
Start at the owning Documentation Map and compare up to three related docs
with the implementation. This is a bounded rotating sample, not a durable
coverage cursor; activations on the same UTC day revisit that area. Record the
selection date with the evidence.

Invoke the repository documentation linter directly during discovery with
`--base <dispatch-sha>`; report structural findings separately from factual
mismatches. Follow DAEMON.md for validation after edits. Choose the strongest
actionable topic from recent-change inspection, the rotating sample or lint
findings; report uninspected areas rather than expanding the sweep indefinitely.

Keep discovery evidence compact: request only needed PR fields (number, state,
author, head branch, title and labels), deduplicate results across searches, and
retain targeted source, diff or review excerpts. Do not reread unchanged pages
or full tool outputs, or search generated lockfiles for documentation evidence.

Only after finding a supported candidate, clear it before publishing:

- Establish the complete current open librarian inventory from every executor
  for the DAEMON.md open-PR and lifecycle caps. Paginate the open inventory to
  exhaustion, including unlabeled legacy work: `librarian` labels, `librarian/`
  heads and legacy `app/charliecreates` (REST login `charliecreates[bot]`)
  proposals on `charlie/librarian-*`, `docs/*` or `chore/*` branches. Branches
  and authors are clues, not proof of role. Inspect changed files and rationale
  only for ambiguous open roles, lifecycle transitions or candidate overlap.
- Search closed, unmerged PR titles/bodies for this candidate's topic, document
  names and area terms, including those legacy authors and branches. GitHub PR
  search does not index changed-file paths: inspect changed files, rationale
  and reviews for the relevant metadata matches. Disclose search terms and
  coverage limits; do not claim exhaustive historical path clearance, enumerate
  all closed documentation/chore PRs, or require a complete librarian archive.
- Do not duplicate an open topic or recreate a closed, unmerged proposal without
  materially new evidence; cite that evidence in the PR body. If candidate
  clearance is unavailable, withhold that proposal and report the specific gap.
  This does not block source review or clearance of unrelated candidates.

Adequate source and lint inspection with no supported edit permits a scoped noop
without archive clearance or open-inventory proof. A claimed cap or overlap
deferral must have verified evidence; unavailable candidate clearance is
incomplete work for that candidate, not proof that there is no work.

Make at most one topical documentation proposal within the librarian's scope.
Use the configured gumnut-bot[bot] author and committer identity. Commit it
in at most three commits and leave no uncommitted edits, then call
the `create_pull_request` safe-output tool once with branch
`librarian/<short-topic>-${{ github.run_id }}`. Derive the short topic
deterministically from the candidate's documentation area using lowercase
letters, digits, `.`, `_`, `-`; preserve that candidate identity on reruns.
Before retrying uncertain publication, reconcile the existing branch and PR.
If the prior outcome cannot be established, stop as incomplete work rather
than create another identity. Select one eligible human using contribution history and record the evidence
in the PR body. Request numeric handoff on a later activation; do not claim that the
PR or review request exists yet. Exclude logins ending in `[bot]` and known automation accounts, case-insensitively:
`CharlieHelps`, `CharlieCreates`, `github-actions`, `gumnut-bot`,
`chatgpt-codex-connector`, and `Copilot`. Do not request teams. Apply the verified
DAEMON.md caps and status-transition limits before publishing; when they leave no
room, report the verified deferral. Use the repository linter directly and
relevant repository guidance to validate any edit. Check whitespace in the
entire candidate change, including new files. If there is no justified edit,
call the `noop` safe-output tool with the dispatch revision, recent commits and
topics inspected, rotating area and document paths, implementation evidence,
linter results, any candidate-clearance findings and coverage limits. Distinguish
checked and consistent from already proposed, cap-limited, or insufficient evidence.
Do not claim repository-wide documentation health from this bounded sample.
A failed required source or validation check is incomplete work. Unavailable
evidence needed to clear a supported candidate prevents its proposal and must
be reported as incomplete candidate clearance, not a successful no-work result.
Choose one native outcome: delivery, completed no-work noop, or report_incomplete for blocked work. Never emit noop alongside a PR proposal. A text-only answer does not
establish a completed no-work run.
If preparation, inference, or validation fails, report that specific phase and
the source revision. A timeout or interruption leaves the proposal incomplete.
The agent job may report that its checkout is clean, but gh-aw's completed
safe-output processing determines whether a valid no-work result exists.

The proposal's PR checks and human review are its gate. Do not push or change
the default branch yourself.

Use native publication only. Keep scope, limits, readiness, deduplication and human
selection in agent policy. Workflow and security-file changes may be proposed
through ordinary human-reviewed PRs when within the role's scope. Native transport
does not upload Git LFS objects; defer proposals requiring new LFS uploads.
Never retry uncertain publication blindly; reconcile the actual branch and PR.

Follow the shared human-review readiness and deduplication policy in
`.agents/daemons/AGENTS.md`. Use native numeric PR targets on a later activation;
never use a fresh-PR temporary alias or hand off code changed in this activation.
