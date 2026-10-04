# Native PR review

`../workflows/pr-review.md` owns the executable workflow, compiled with
`github/gh-aw` v0.89.21 at commit
`c35393777e5604a63721d09512263b1383301d4f`. After compiling with
`--action-tag c35393777e5604a63721d09512263b1383301d4f`, run
`uv run python .github/pr-review/reviewer-compile.py` to apply the pinned compiler's
missing detection-image digests, duplicate activation dependency and native sanitizer environment parity, then
`node --test .github/pr-review/reviewer.test.cjs` and `actionlint` on the two
PR-review YAML files. The CI contract test also checks out that exact native commit and runs
`GH_AW_ACTIONS_DIR=<native-checkout>/actions/setup/js node --test .github/pr-review/reviewer*.test.cjs`,
exercising native collection, the trusted guard, native publication and readback. Commit the source and lock together. The reviewed daemon
and its lanes remain the review-policy authority.

The repository variable `PR_REVIEW_ENABLED=true` admits inference. Missing or
false disables review execution. Before enabling it, configure `CODEX_API_KEY`,
install the publisher App with Pull Requests read/write access, and set
`PR_REVIEW_APP_CLIENT_ID` and `PR_REVIEW_APP_PRIVATE_KEY` in the `pr-review-publisher` environment.
Set repository variable `PR_REVIEW_BOT_LOGIN` to the exact observed
`<app-slug>[bot]` login. This App must be separate from the PR-creation App.
That environment must restrict deployments to the reviewed default branch.
Do not widen the environment to contributor branches to make a test pass.
No publisher credential enters the inference sandbox; the native firewall
holds provider credentials outside that sandbox. The GitHub inference tool
uses the read-only workflow token explicitly.

Automatic review covers open non-draft PRs, readiness, reopen and head updates.
A reviewer request for `CharlieHelps` or `the configured reviewer App bot`, a complete `/review`
or `@CharlieHelps review` command line, and default-branch manual dispatch are
supported. A separate read-only comment-admission workflow hands off numeric
PR/comment IDs through authenticated run metadata. The reviewer re-fetches the
source run, exact workflow path, comment and author permissions before inference;
comment jobs have no publisher credentials. Manual requests require the caller's current repository write,
maintain or admin permission. Other free-form Charlie mentions do not trigger
paid inference. Opening fork PRs can trigger inference once enabled; rate/cost
exposure remains an operator decision. No contributor checkout, dependency
installation or contributor script execution is permitted in these jobs.

Admission resolves live current-head metadata and skips completed same-head
reviews by the publisher. Serialized runs converge from that live state rather
than queued event snapshots. The native publisher pins the formal review and
inline comments to the admitted commit. A head change before publication fails
closed; a race after that check can only attribute the review to the old head,
and delivery readback fails the run until the new head is reviewed. An explicit
plain coverage declaration survives native collection. Collector validation errors
fail closed before publication; trusted code then adds durable coverage and delivery
markers. The publisher
validates finding paths against the PR diff and adds a compact inline delivery
manifest. Delivery and duplicate admission compare every declared finding with
the comments on that formal review; dropped or altered findings remain
incomplete and do not suppress a retry. Blocking
findings produce COMMENT, clean completed reads APPROVE, and incomplete reads
COMMENT with a limitation. The publisher cannot approve its own authored PR;
those require a different reviewer. A run is successful only after a complete
formal review from that run is independently read back at the current head.

Before cutover, validate on merged default-branch policy with separately
admitted paid tests: complete clean approval, blocking COMMENT with inline
finding, limitation COMMENT, exact-head changes, duplicate events, authorized
and unauthorized requests, a fork with adversarial text, and failed inference /
publication. Check the actual hosted Charlie installation before withdrawing
its coverage. These files do not disable Charlie. Without successful runtime
verification, the replacement remains unverified even if CI is green.

Native AIC is an inference estimate, not a provider invoice or portfolio cap.
The workflow source owns per-run/daily limits; manual dispatch bypasses the
native daily guard. Record PR creations, head updates and manual requests,
then combine native run accounting with per-job runner estimates and actual
provider invoices. Review admission denied by a budget is uncovered work and
must be handled through a human review or a separately admitted retry. Do not
claim the monthly goal from per-run settings.
