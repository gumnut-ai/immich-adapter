# Scheduled documentation librarian

`.github/workflows/librarian.md` is the reviewed source;
`librarian.lock.yml` is its executable native gh-aw workflow. The librarian
reads the dispatch revision and `.agents/daemons/librarian/DAEMON.md`, samples
recent changes plus one adapter area, and proposes at most one documentation
PR. The existing policy owns lifecycle evidence, scope, three-open-PR and
one-transition caps, three commits, and human reviewer selection.

## Provisioning and activation

The workflow remains disabled unless repository variable `LIBRARIAN_ENABLED`
is exactly `true`. Provision before enabling it:

- Repository secret `CODEX_API_KEY`: the OpenAI API credential for isolated
  inference and threat detection. The agent receives read-only GitHub access.
- Environment `gumbot-publisher`: variable `GUMBOT_CLIENT_ID` and secret
  `GUMBOT_PRIVATE_KEY` for the installed gumnut-bot GitHub App. Install the App
  on this repository with contents and pull-request write permissions; it
  must be able to push proposal branches, open PRs, and request human reviews.
  The App private key is available only to native publication/conclusion jobs.
- Blacksmith access for the configured inference and detection runner.

Keep the gate disabled until an operator admits one bounded manual validation
on reviewed default-branch code and inspects native completion, costs, and any
published proposal. The dispatch rejects additional prompt context and branch
overrides. Do not enable scheduled execution solely because compilation or
ordinary CI passes. Hosted-daemon coverage and overlap must be verified
separately before retiring the prior executor. Setting the variable to `false`
stops new admissions; cancel any admitted run separately if needed.

## Runtime evidence

Native safe outputs own proposal/noop completion and publication. The delivery
job reads back the bot author and committer identities, one recorded human
review request, and registration of `.github/workflows/ci.yml` on the published
HEAD. That handoff does not mean CI passed or the human approved the proposal.
Keep incomplete publication visible for maintainer follow-up.

Native AI credit limits are estimates, not provider invoice ceilings. The source
owns inference, detection and rolling daily admission limits; manual dispatch
skips the native daily admission check. Include inference, detection, runner,
failed and no-op runs in the shared monthly automation budget. Native admission
does not enforce that portfolio budget. Check the provider invoice and native
cost metadata, including the documented long-context pricing limitation.

## Recompilation and validation

Compile with gh-aw v0.89.21 and `--action-tag v0.89.21`. Apply the generated-file
pin corrections documented at the top of the source, then run:

```bash
uv run pytest tests/test_librarian_workflow.py
node --test .github/librarian/verify-delivery.test.cjs
actionlint .github/workflows/librarian.lock.yml
uv run --no-config --locked --script scripts/lint_docs.py --base origin/main
git diff --check
```

Use the repository's pinned uv version from `AGENTS.md`. Tests check native
budget wiring and abort handling, immutable firewall pins, isolated publisher
credentials, docs-only publication scope, and incomplete delivery behavior.
They do not prove live App access or sandbox setup.
