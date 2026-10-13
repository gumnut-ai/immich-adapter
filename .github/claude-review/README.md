# Subscription-backed Claude PR comments

`../workflows/claude-review.yml` adds an independent Claude perspective alongside
the existing hosted Codex reviewer. It uses Anthropic's Claude Code Action and
Claude Opus 5.5 at medium effort, and posts inline findings plus a compact
summary directly on the PR as `claude[bot]` through the official Claude GitHub
App. It does not approve, request changes, merge, or fix code. The inline tool
may create a `COMMENTED` GitHub review to attach findings. The disabled native reviewer remains separate and unchanged.

The new reviewer is **disabled until explicitly enabled**. Static checks do not
verify subscription authentication, model access, review quality, or delivery.

## Coverage and trust

Once enabled, opened, synchronized, ready-for-review, and reopened events review
open non-draft PRs targeting the default branch. Initial automatic coverage is
same-repository PRs triggered by human collaborators with write access or
authenticated GitHub Bot senders with exact `<slug>[bot]` identities. Forks and
other target branches are excluded. Failed or skipped runs leave review
to Codex or a human; this workflow is not a required approval gate.

The metadata-only `pull_request_target` job checks the initiating human's write
access or the authenticated Bot identity and dispatches this workflow at the
default branch. It has no OAuth secret or OIDC permission. The `workflow_dispatch` review job checks live PR eligibility,
head freshness, and the originating run's repository, workflow, PR head, and
admitted actor before accessing OAuth. A manual default-branch dispatch is also available
to human writers; supply `pr_number` and leave the origin inputs blank.
Both checkouts use the exact trusted event SHA, with persisted credentials disabled;
contributor code, configuration, hooks, and dependencies are never executed.
Claude discovers and fetches PR context as needed through GitHub tools, including
commit history, individual commit diffs, and related code search. Indexed search
results are discovery hints; relevant files must be read at the recorded SHAs.
The workflow overrides the action's bundled GitHub MCP v0.17.1 with the
digest-pinned v1.12.2 release, already used by our native workflows. The action's
later inline MCP configuration wins by server name and inherits its exchanged
Claude App token. The server exposes only seven named GitHub tools; the helper
has the six read tools, and only Opus can post comments.
The consolidated `pull_request_read` tool paginates files, commits, reviews, and
comments with `page`/`perPage`, and review threads with `perPage`/`after`.
Policy starts thread reads at five per page and follows `pageInfo` cursors.
This controls thread-page size, not every response: full diffs and individual
files can still be large, patches can be omitted/truncated, and each returned
thread includes at most 100 comments. Use targeted reads and report material
coverage gaps; pagination does not establish complete coverage by itself.
Restricted mode ignores
user/project settings, strict MCP configuration limits servers to those supplied
by the workflow's pinned GitHub server and the action's existing inline-comment
server, and the built-in tool set only allows MCP discovery and the
named helper and guarded `Read`.
Review policy/code reads go through GitHub at explicit SHAs;
native `Read` can recover oversized MCP responses saved in the current session's
`tool-results` store. A trusted `PreToolUse` guard rejects other files, path
escapes, and symlinks; a native deny rule also blocks checkout reads. Hook failures and timeouts
also block Read through `onFailure: "block"` (requires CLI 2.1.295+). Both agents inherit that guard and can page saved results
with `offset`/`limit`; shell and file-writing tools remain unavailable. The optional `haiku-helper`
uses Haiku 5.5 with GitHub reads and the same scoped `Read` tool; Opus owns the assessment and posting.
The helper cannot publish or spawn other agents. This also keeps
the action's `.git/config` credential outside the model's readable surface.
`dontAsk` permits only explicitly allowed MCP reads, comment tools, and the
named helper and guarded `Read`; other mutations, including formal review submission, are denied.
The job grants OIDC only to exchange for the installed official Claude app
token. `additional_permissions` requests `contents: read`, overriding the
action's default write scope; PR/issue comments remain writable. No custom
publisher or second adjudicating reviewer is used. The routing script reports a
`Claude PR review` execution check on the actual PR head; it succeeds only when
the job and inference both succeed. This check is not approval. Cancellation or
a runner interruption can leave an in-progress check if finalization cannot run.

Anthropic's app-token exchange currently rejects our `pull_request_target` OIDC
token ([upstream #713](https://github.com/anthropics/claude-code-action/issues/713)).
Inference therefore uses `workflow_dispatch`; accepting a bot-started dispatch
and delivering app-authored comments must be verified after merge. The narrowly
allowed `github-actions` invoker still requires validated originating metadata.
There is no workflow-token publishing fallback.

Review policy is fetched at the trusted default-branch dispatch SHA. Changes to this
workflow or policy only take effect after merge. Each new head cancels older pending/running
reviews for that PR. The prompt checks freshness before comments and pins inline
comments to the reviewed head. Direct publication still has a small race between
the check and a concurrent push; comments name their reviewed revision and may
become outdated. This is advisory feedback, not a trusted current-head approval.
The Claude-specific policy leads with one clear outcome sentence: looks good
with no issues found, has concerns, or review incomplete. Short bullets follow
only for important concerns, prior-finding status, or material coverage limits;
a clean review needs no implementation walkthrough. Follow-ups avoid repeating
resolved or identical findings, and the summary retains head/base attribution.
These review and freshness behaviors are model instructions and require pilot
verification.

## Provision and pilot

1. Install the [official Claude GitHub App](https://github.com/apps/claude) for
   this repository. The action exchanges the job's OIDC token for its publishing
   identity; do not set `github_token`, which would override the app identity.
   Use a subscription with Claude Code and the selected Opus model available.
   Run `claude setup-token` locally while signed in to that subscription. Save
   its output directly as the repository Actions secret
   `CLAUDE_CODE_OAUTH_TOKEN`; never paste it into chat, an issue, or a PR. The
   token uses that subscriber's capacity and must be rotated on expiry/revocation.
2. Merge and review the default-branch workflow before enabling it. Set the
   repository Actions variable `CLAUDE_PR_REVIEW_ENABLED` to the literal `true`
   for an explicitly supervised pilot. Missing/false disables jobs; an enabled
   workflow with a missing token fails before invoking Claude.
3. Open a small non-draft PR, then push a correction. Verify independent inline
   feedback at the recorded commit, summary delivery as `claude[bot]`, clean
   versus incomplete reporting, and no duplicate findings on the follow-up. Verify both human manual dispatch and automatic dispatch from an eligible PR event.
   Check cancellation during another head update, and confirm forks/drafts/bot-only triggers skip.
   After merging a workflow fix, trigger a fresh eligible PR event. GitHub's
   **Re-run jobs** uses the original event's workflow revision, SHA, and ref;
   it does not pick up the merged fix. Check the run's trusted policy SHA and
   arguments before interpreting its result.
4. Inspect the action's resolved model, conclusion, turns, duration, and the
   subscriber's usage. On failure, check **Report safe failure category** for an
   allowlisted SDK error category. Unknown, missing, malformed, or oversized
   execution output yields a generic category. This diagnostic emits no
   transcript or error text; the upstream action may still log its own errors.
   Verify optional helper reads and denied publication/local tools.
   Keep full-output/debug logging off because it exposes tool results.
   Each run is limited to 50 turns and 20 minutes. These are
   runtime limits, not a dollar cap or a guarantee of subscription availability.
   Other Claude Code sessions share capacity. Leave subscription extra usage off
   if you want exhaustion to stop work rather than bill beyond included usage.
5. Set `CLAUDE_PR_REVIEW_ENABLED=false` to stop future jobs. Separately cancel
   any active run in GitHub Actions if you need an immediate stop. Rotate/revoke
   the token if necessary. A successful
   inference log alone does not prove comments reached the PR: read them back.

There is no API-key fallback and no hosted Claude Code Review charge. Subscriber
API credits are a separate allowance and are not consumed by this OAuth route.
Do not add `--bare`: it ignores OAuth credentials and requires API authentication.
Opus 5.5 requires Claude Code 2.1.280 or later. The pinned action bundles
CLI 2.1.295 and Agent SDK 0.3.295; verify both when updating the action.
If subscription capacity proves unsuitable, explicitly switch authentication
and budgets to API credits in a later change. An exhausted/expired token or
unavailable model should fail visibly rather than silently change billing.

The summary includes a structured `clean`, `concerns`, or `incomplete` verdict
with the reviewed head/base for the separate
[standing approval coordinator](../pr-approval/README.md).
Claude still posts advisory comments only.

## Usage reporting

Eligible review runs always attempt to write a **Claude review usage** job
summary and a `claude-review-usage` artifact retained for 30 days. The trusted
helper projects only allowlisted status labels and finite numeric metrics from
the official action's saved SDK result; the execution transcript is never
uploaded by this workflow, and full-output logging stays off. Missing,
malformed, oversized (over 8 MiB), or unavailable output is reported explicitly.
This reporting does not turn an authentication or inference failure into success.

The artifact includes turns, SDK duration, permission-denial count, estimated
cost, and input, output, cache-read, and cache-creation tokens by model and in
total. Totals use the latest cumulative `modelUsage` result, including Opus and
any Haiku helper calls through the SDK query pipeline; result events are not
summed. The SDK's separate `usage` field excludes helpers and is not used for
these totals. Auxiliary calls outside that pipeline are excluded. Unknown model
names are replaced with bounded labels, and invalid numeric fields stay
unavailable rather than becoming zero.

Estimated USD is a list-price equivalent when the SDK reports list pricing;
other pricing bases are identified as mixed or unknown. It is not a
subscription charge or API invoice. Execution status, usage availability,
comment delivery, and coverage are separate: the usage report cannot verify
that comments reached the PR or that Claude completed the intended coverage.

## Checks and upstream references

The credential-free contract tests run in the existing PR workflow checks job:

```sh
node --test .github/claude-review/*.test.cjs
actionlint .github/workflows/claude-review.yml .github/workflows/pr-review-tests.yml
zizmor .github/workflows/claude-review.yml
```

- [Claude Code GitHub Actions authentication](https://code.claude.com/docs/en/github-actions)
- [Model compatibility and minimum CLI versions](https://code.claude.com/docs/en/model-config)
- [Bare mode authentication](https://code.claude.com/docs/en/headless#start-faster-with-bare-mode)
- [GitHub workflow reruns](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs)
- [Claude Code permission rules](https://code.claude.com/docs/en/agent-sdk/permissions)
- [Subscriber API credits](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers)
- [Pinned action source](https://github.com/anthropics/claude-code-action/tree/2dca132ff0e0c4094ce6048b422c6915a071210b)
- [Pinned GitHub MCP release](https://github.com/github/github-mcp-server/releases/tag/v1.12.2)
- [Native PR reader and pagination](https://github.com/github/github-mcp-server/blob/v1.12.2/pkg/github/pullrequests.go)
