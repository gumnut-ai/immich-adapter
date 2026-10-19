---
title: Production API Smoke Testing
last-updated: 2026-10-19
---

# Production API Smoke Testing

Use this guide to provision and operate the headless catalog in
[`smoke-tests/`](../../smoke-tests/README.md). A hosted check reads the deployed
adapter over HTTP, exercising its credential, Gumnut API connection, and
Immich response mapping together. Local tests cannot establish that production
path. A deployed definition cannot establish a passing hosted result.

## Ownership and custody

Record the named production smoke operator and backup in the private operating
issue before activation. They own provisioning, quota review, failure triage,
and the launch audit. Failures use the existing shared production Slack
channel; resolve its Checkly channel ID from the account.

Create a separately named adapter-smoke Gumnut API key for the safe demo
identity. Grant only the read permissions needed for current-user, library
resolution, and album metadata. Prefer a key scoped to the demo library;
the identity's selected Immich library must be covered by that key. Verify
the key against the deployed adapter before activation. Do not broaden
permissions or change the identity's library selection to make the check pass
without a separate decision.

Store the key in the shared production password manager, the adapter
repository's Actions secret `IMMICH_SMOKE_API_KEY`, and the Checkly group's
encrypted secret of the same name. The workflow maps the GitHub value to
Checkly's deployment-time `CHECKLY_SECRET_IMMICH_SMOKE_API_KEY`; the runtime
name remains `IMMICH_SMOKE_API_KEY`. Never put a literal key in commands,
source, issues, or result artifacts. Restrict check-code administration to
operators because check code can access runtime secrets.

Use a separately named Checkly service API key in the repository Actions
secret `CHECKLY_API_KEY`. It deploys definitions and is distinct from the
read-only runtime key. Record its consumers and rotation owner privately.
Provisioning is an operator action; the catalog creates no identities,
libraries, albums, credentials, or alert channels.

## Fixture and configuration

Choose a small, stable, nonempty album containing only safe demo content.
Verify its expected Immich user UUID, album UUID, title, and exact positive
asset count against the deployed endpoints. Pin the fixture rather than
discovering an arbitrary album each run; a missing or emptied fixture must
fail. The single-album endpoint reads metadata without enumerating assets.

Configure these repository Actions variables:

| Variable | Value |
|----------|-------|
| `CHECKLY_ACCOUNT_ID` | Existing production monitoring account ID |
| `CHECKLY_SLACK_CHANNEL_ID` | Existing shared production Slack alert channel ID |
| `IMMICH_SMOKE_USER_ID` | Expected Immich UUID for the demo identity |
| `IMMICH_SMOKE_ALBUM_ID` | Expected Immich UUID for the fixture album |
| `IMMICH_SMOKE_ALBUM_NAME` | Exact safe demo album title |
| `IMMICH_SMOKE_ASSET_COUNT` | Exact positive integer asset count |
| `IMMICH_SMOKE_DEPLOY_ENABLED` | `true` enables definition deployment from `main` |
| `IMMICH_SMOKE_SCHEDULE_ACTIVE` | `true` activates scheduling; leave `false` during provisioning |

Use only nonpersonal demo values. The catalog fixes the adapter origin in
source so an environment override cannot redirect the production key.
Local execution uses the same fixture names and `IMMICH_SMOKE_API_KEY`.
Construct loading uses `SMOKE_SCHEDULE_ACTIVE` for activation and Checkly's
`CHECKLY_SECRET_` prefix for the runtime credential.

The [workflow](../../.github/workflows/production-smoke.yml) validates fork
pull requests without secrets or production traffic. Deployment runs only from
`main`, when explicitly enabled, preserves other projects, and does not
schedule a run on deployment. Manual workflow dispatch applies the current
`main` definition and variables. A later deployment can undo an operator pause
if the repository variable still requests activation.

## Validate and activate

Use the existing Checkly native schedule when measured combined usage fits
the safety gates. It keeps result retention and immediate failure/recovery
alerts on the established runner. Another runner needs its own missing-run
detection, secret custody, retained evidence, and verified alert integration.
Compare measured cost and operator burden before changing runner, plan, or
existing monitor cadence.

Before enabling scheduling, retain these records in the private operating
issue:

- Deployed adapter revision, fixture verification, credential name/ID and read
  scopes, operator/backup, and custody locations. Never retain the key or raw
  user/album response.
- A passing **recorded hosted** run of this exact catalog and fixture.
  Use `checkly test --record` with private environment injection. A local run
  or `checkly test --list` does not count.
- Billing-period dates and counters before and after an isolated hosted run,
  its billed request delta, and other traffic that could confound measurement.
  If the delta cannot be isolated, record that uncertainty and measure again.
  The two adapter HTTP requests exclude its upstream calls, which still belong
  in service-load assessment.
- Combined monthly API/Multistep and browser projections, including manual
  runs and alert drills. Preserve the existing safety gates of **8,000 API
  requests and 800 browser units per month**. Two requests hourly add 1,440
  per 30 days or 1,488 per 31 days, in one location with no retries. Account
  for month length and billing dates. Do not upgrade the plan or change another
  catalog's cadence implicitly.

After these prerequisites pass, set `IMMICH_SMOKE_SCHEDULE_ACTIVE=true` and
deploy from `main`. Verify the hosted group and check are active, unmuted,
and use the intended hourly schedule, location, no-retry policy, and
first-failure alert channel. Observe the first scheduled result separately
from manual test sessions.

Perform an attended failure/recovery drill with a temporary incorrect expected
fixture value, leaving credentials and production data untouched. Restore the
correct value immediately after observing the first failure. Confirm both
failure and recovery messages reach the intended Slack channel; retain their
links with hosted result URLs and UTC timestamps. A channel test notification
alone does not prove the check's alert policy. Missing alerts leave delivery
unverified and require investigation before activation is declared complete.

## Pause, rotate, and resume

Set `IMMICH_SMOKE_SCHEDULE_ACTIVE=false`, deploy from `main`, and verify the
hosted group is inactive with no scheduled run in progress. Emergency UI
deactivation is valid; synchronize the repository variable before another
deployment. Muting suppresses notifications while requests continue, so it
is not a maintenance pause.

For planned key rotation, pause, create a replacement with the same narrow
scopes, update the password manager and GitHub secret, and deploy the Checkly
secret while inactive. Validate a hosted run before revoking the old key and
resuming. If compromise is suspected, revoke the exposed key immediately;
do not wait for successful replacement. Rotate the Checkly service key
separately if affected. A pause cannot prevent use of a stolen credential.

For fixture maintenance, pause before changing the demo corpus, recheck the
deployed mapping, and update expectations. Resume after a recorded hosted run
passes and combined quota headroom remains adequate. Verify the next scheduled
result and record pause/resume times and operator. Never silently replace
the fixture with whichever album passes.

## Failures, gaps, and the seven-day audit

Errors contain endpoint labels, HTTP status, and failed contract paths only.
Never add raw responses, authorization headers, names, emails, or SDK exception
dumps. Keep investigation records similarly sanitized; do not enable payload
capture or browser traces for this catalog.

Record each failed result's URL, UTC time, manual/scheduled source, location,
assertion or transport category, adapter deployment, and alert link. Compare
adapter logs and upstream health before attributing a failure. Classify it as
product, infrastructure/provider, credential, fixture/test, or unresolved,
with supporting evidence and investigation time. A passing rerun alone does
not establish a flake. Rollback is a separate human production decision;
the catalog performs no remediation or cleanup.

For a missing scheduled result, compare cadence and hosted history, allowing
for documented pauses. Check activation, deployment, provider health, and
billing usage. Notify the production responder of the gap and check the
adapter path independently. Record uncovered UTC slots, cause or uncertainty,
and the next observed scheduled result. A last green result does not establish
current coverage. Do not reduce cadence, suppress alerts, or upgrade billing
automatically.

Begin the seven-day audit at the first verified scheduled slot after the
alert drill. Retain evidence daily before provider detail history expires.
The final record needs the exact UTC window, expected and observed scheduled
slots (manual sessions excluded), pauses/gaps, failure counts by classification,
repeated signatures, alert/recovery links, measured usage and projection,
incidents caught or missed, and investigation time. Record explicit operator
sign-off or unresolved follow-up. The issue stays open until activation,
alert evidence, and this audit are complete.
