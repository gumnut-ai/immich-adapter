# Standing PR approval pilot

The coordinator runs automatically once its workflows and reviewer App are
installed and its protected publisher credentials are configured. No separate
enable setting is required. It combines existing Claude and hosted Codex
reviews, adds no model inference, and never merges a PR. Static tests do not
prove GitHub counts its approval toward the repository's ruleset.

## Accepted trust boundary

This pilot trusts recognized feedback posted by the existing Claude
and Codex GitHub bot accounts, including Codex's native PR-level thumbs-up.
Their identities are shared with interactive
assistants, so identity and text do not prove an independent review workflow
produced the feedback. Repository writers are trusted not to manufacture
sign-offs through those assistants. This is an accepted pilot limitation;
the coordinator does not attest review origin or add a separate Codex producer.

## Decisions

Sign-offs belong to the PR. Claude and Codex can sign off on different revisions,
and neither sign-off expires merely because the head or base changes. An existing
coordinator approval likewise persists after pushes. The coordinator does not
change the repository's stale-approval settings or claim both reviewers read
the current diff.

The Claude summary's structured verdict records `clean`, `concerns`, or
`incomplete`, with the reviewed head/base. A later significant concern supersedes
a clean sign-off; a later incomplete attempt does not itself erase a standing
decision. Legacy free-form Claude summaries are not automatically interpreted
as clean: the pilot needs a structured verdict once. When GitHub timestamps
cannot order same-second decisions, a clean verdict does not override a concern.
Codex's authenticated thumbs-up on the PR itself is a standing sign-off. GitHub
can report its reaction actor as `User`; the coordinator matches the immutable
account ID to an authenticated Codex `Bot` comment or review. Reactions on
comments do not qualify. A PR-level reaction does not report a reviewed commit,
so the approval explanation says that explicitly.
Codex's explicit clean headline with a reviewed commit is also accepted; human
closing text on that headline can vary. A completed formal suggestions review
whose associated findings are all P3 also qualifies as clean when its wrapper
and commit match. P0/P1/P2 findings, mixed or unclassified findings, and unknown
body text do not qualify. Its activity table's `Completed` or `Running` status
alone is not a sign-off and does not erase an earlier decision.
Formal source approvals count with empty or
recognized clean bodies; contradictory or unknown bodies block. Active formal
approvals and changes requests follow submission order; editing a review body
does not resubmit its disposition. Dismissing a source or human review removes
only that review, preserving other active dispositions and independent sign-offs. Significant
unresolved findings, including outdated threads, and
human requests for changes prevent approval. Advisory findings do not.
Unrecognized nonempty reviewer comments or formal review bodies also block;
a later understood clean decision can clear that ambiguity. Unclassified
unresolved reviewer threads must be resolved or clarified as advisory.
Recognized Codex activity-summary metadata is informational, and an explicit
Claude `incomplete` verdict preserves the earlier decision. Neither means clean.
The hosted Codex clean-comment format is observed behavior, not a guaranteed
external schema; changed output must be verified before extending the parser.

Initial approval requires both standing sign-offs and successful reported
current-head checks, statuses, and workflow runs. Active workflows wait even when
some checks have already passed. This observes GitHub's registered CI; it does
not define a new mandatory workflow inventory or change the repository ruleset.
Missing or unsuccessful CI waits. Later pushes or incomplete reviews do not
withdraw an existing approval. A later significant concern causes the coordinator
to submit `REQUEST_CHANGES` if it previously approved; unrecognized reviewer
feedback likewise withholds approval with a clarification reason. After it clears,
both standing sign-offs and current CI allow approval again.
An explicit dismissal of the coordinator's review pauses reapproval until at
least one reviewer supplies a clearly later clean sign-off. A push alone cannot
undo that dismissal.

The same conditions apply to human- and bot-authored same-repository PRs targeting
the default branch. Drafts, forks, closed PRs and other targets are excluded.
The publisher cannot approve its own authored PR, so use `gumnut-reviewer` as
the reviewer App, separate from the PR author App `gumnut-bot`.

## Routing and publication

The read-only request workflow accepts eligible human or bot PR events,
authenticated Claude/Codex summary changes, formal reviews, and bot findings.
Ordinary top-level comments cannot start it.
Default-branch manual dispatch requires a human with write/maintain/admin access.
Review/comment-event workflow definitions can come from the PR merge commit;
their read-only token and withheld secrets are not publication authority. The
source's scripted checks read default-branch files. The privileged deferred
coordinator executes its own default-branch workflow, re-fetches the source run
and live PR, and derives decisions independently. Request routing requires a
successful admission step from that exact run attempt; successful no-op and
skipped request runs do not route an assessment. No source artifact, contributor
checkout, dependency installation, or comment-controlled code enters publication.
Completed CI runs associated with one PR also request reassessment.
Runs with no associated PR or multiple associated PRs need a manual request if
no later reviewer event supplies one. Scheduled reassessment also covers review
thread resolution, which is a webhook but not an Actions trigger, and missed
event handoffs. It examines open eligible PRs without starting inference.
An unavailable assessment abstains for that PR, reports the partial failure, and
continues with other PRs.
Pending runs can be coalesced; publication
always evaluates live evidence again in a serialized per-PR job.

The read token collects reviews, comments, threads, and CI separately from the
publisher token. Only the protected default-branch publisher job receives the
App private key; its short-lived installation token requests Pull Requests write
permission only. A durable pending formal review records publication intent
before submission. Ambiguous pending intent or failed readback requires operator
inspection, rather than automatic resubmission. Every submitted review is read
back for its identity, state, commit and exact body. Its body links the original
sign-offs and publication run, preserving the distinction between reviewed and
publication commits.
The approval review body explains the mechanical decision: links and timestamps
for both source sign-offs, their reviewed commits, current-head CI evidence and
observed counts, blocker checks, and the standing-approval policy. It explicitly
states the shared-bot trust assumption and that the coordinator did not
independently review the code.

The pilot adds one request job for supported review events, one assessment job
for associated completed runs, and a publication job only when the formal
decision needs changing. These are billed per-job runner minutes, not model
usage. Assessments run automatically; the publisher job is admitted only when
a formal decision needs changing and uses the protected App credentials.
Scheduled assessments allocate one runner job per admitted tick in addition to
event-driven jobs; GitHub schedules can be delayed. The workflow owns the interval.

## Provision and verify

1. Merge the reviewed workflow and policy and install the separate
   `gumnut-reviewer` GitHub App on this repository with Pull Requests write and
   Contents write. Pull Requests write alone can submit an `APPROVED` review,
   but GitHub's required-review count needs repository-writer access. Keep the
   publisher's repository-scoped runtime token limited to Pull Requests write.
   The App key can mint broader code-write tokens, so retain its protected custody.
   The caller defaults to `gumnut-reviewer[bot]`; set `PR_APPROVAL_BOT_LOGIN`
   only when using a different App, to its exact observed `<slug>[bot]` identity.
   Configure numeric `PR_APPROVAL_APP_ID` and `PR_APPROVAL_APP_PRIVATE_KEY` in
   the `pr-approval-publisher` environment, restricted to the default branch.
   Keep secret values out of issues, PRs and chat. A missing installation or
   credential cannot publish; token creation must succeed for this repository.
2. Configure hosted Codex automatic review for **All PRs** and the independent
   Claude reviewer. Verify a bot-created PR receives both sign-offs; a bot-authored
   trigger comment is not assumed to work. Once the installation and credentials
   are ready, eligible events and scheduled assessments can publish without
   another activation step. There is no shadow/publication mode variable.
3. On a small human PR and a `gumnut-bot` PR, read back the reviewer feedback,
   coordinator assessment and formal review. Verify retained sign-offs after a
   push, significant-concern withdrawal and clean reapproval, incomplete and
   unknown-output withholding, explicit dismissal, duplicates, rejected
   self-approval and failed/ambiguous publication. Record PR/run URLs, source
   feedback links and commits, expected/observed decisions and manual intervention.
   Check the actual ruleset decision, not only an `APPROVED` API response.
4. Disable the approval request and coordinator workflows through GitHub Actions
   to pause new work; cancel active runs separately for an immediate stop.
   Removing the App installation or revoking its credentials prevents future
   publication. Neither action dismisses approvals already submitted. Reconcile
   any pending publication intent before retrying.

## Shared implementation

The coordinator and request routing are maintained in
[gumnut-ai/automation](https://github.com/gumnut-ai/automation), using one reviewed
immutable revision for approval actions and their regression suite. Claude review
and native toolchain pins can advance independently. The canonical
[standing approval policy](https://github.com/gumnut-ai/automation/blob/c14c5a06778622f4221aa8aa5114ece628494858/README.md#standing-approval-policy)
describes the shared decision rules. This repository retains event sources,
schedules, permissions, App credentials and the protected publisher environment.
Shared implementation tests run in the automation repository.

Checks: `node --test .github/pr-approval/*.test.cjs .github/claude-review/*.test.cjs`,
`actionlint` and `zizmor` on the approval and Claude workflows.

## Runner qualification

The request, assessment and publication jobs use `ubuntu-slim`. The isolated
reviewer retains its existing runner. Rerun the read-only
[runner validation workflow](../workflows/approval-runner-validation.yml) with
`gh workflow run approval-runner-validation.yml --ref main`. It compares five
independent jobs per runner using the pinned shared approval tests and no-op
composite assessment, without publisher credentials. It also runs when its own
workflow file changes in a PR.

Synthetic publication tests verify submit/readback and duplicate handling with
API fixtures; they do not establish a counted GitHub approval. After merging a
runner change, verify a real admitted request, assessment and protected publisher
run, read back the review and ruleset result, and compare live job durations and
per-job rounded costs. Keep the default-branch publisher restriction and scoped
App credentials intact. Restore the previous runner labels if acquisition or
publication fails; inspect pending publication intent before any retry.
