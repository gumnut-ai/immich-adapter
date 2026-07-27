# Simplicity

Ask one core question: **why is this pull request this complicated, and how much complexity can be removed while preserving the behavior it actually needs?** Prefer the simplest straightforward implementation that preserves needed behavior, not merely the one with the fewest lines; treat file, line, and abstraction counts only as clues.

This lane uses the advisory standard § Findings and verification permits for `🟠 non-blocking` findings: the concrete issue is the implementation itself, and the material consequence is the ongoing cost of understanding and maintaining it. A finding here does not require a defect, a quantified or high-confidence cost estimate, or a fully specified replacement. The **Evidence** items below say what to show, not a proof gate to clear before speaking.

## Use this lane when

- Use when the pull request introduces substantive implementation — new abstractions, indirection, plumbing, configuration, registries, schemas, extension points, or compatibility surfaces, or a multi-part implementation whose pieces interact.
- Skip when the change is mechanical or small enough that no meaningfully simpler alternative exists — a dependency bump, a rename or move, a formatting or lint fix, a docs-only change, a generated or vendored artifact, a localized edit to existing logic, or an addition that follows an established neighboring pattern without adding new structure.
- Skip when the complexity is pre-existing and the pull request neither introduces nor materially enlarges it.
- Skip on a re-review when this lane already published a finding on this pull request, unless the changes since then introduce a materially different complexity shape. An advisory finding an author read and declined is not resolved, dismissed, or accepted, so nothing else retires it — repeating it each review round is noise.

## What this lane should catch

- **Production plumbing a direct solution does not need**
  - **Report when:** The pull request introduces a service, queue, deployment, background job, or comparable production machinery where a direct local solution would preserve the behavior actually needed.
  - **Evidence:** Identify the introduced plumbing, the behavior it serves, and the simpler local shape that would serve the same behavior.

- **Global machinery where a local canonical source suffices**
  - **Report when:** The pull request adds global configuration, a catalog, registry, schema, or type machinery to express something a single local canonical source could hold.
  - **Evidence:** Point to the added machinery and the narrower place the same information could live.

- **Surface expanded without a present need**
  - **Report when:** The pull request expands schemas, optional parameters, extension points, or compatibility surfaces for a use that does not exist yet in this repository.
  - **Evidence:** Identify the added surface and show that no current caller, consumer, or requirement exercises it.
  - **Do not report:** A surface an applicable repository requirement, a linked issue, or a committed consumer already calls for.

- **Speculative handling that enlarges a direct solution**
  - **Report when:** Speculative error, security, reliability, scale, or production handling substantially enlarges what would otherwise be a direct implementation, without a present trigger.
  - **Evidence:** Identify the speculative handling and its size relative to the core change.
  - **Do not report:** Handling that a demonstrated risk, an applicable repository requirement, or reachable behavior in this change makes necessary — that belongs to `correctness` or `repository-guidance`.

- **Indirection that obscures locally readable logic**
  - **Report when:** Abstractions, wrappers, factories, helpers, or layers of indirection make logic harder to follow than an inline or more direct expression would.
  - **Evidence:** Trace what a reader must follow to understand the behavior, and sketch the more direct shape.

- **Literal requirement reading that overshoots needed behavior**
  - **Report when:** A literal interpretation of a requirement produces implementation scope beyond the behavior people actually need.
  - **Evidence:** Contrast the implemented scope with the behavior the linked issue or description asks for.

These are calibration categories, not a checklist, and none is an automatic objection. Preserve complexity that is clearly required by current behavior or constraints.

## Reporting

- Every finding from this lane is `🟠 non-blocking`. This lane never withholds approval and never produces a `🔴 blocking` finding.
- Publish it inline rather than in the review body, anchored to the most representative changed line — usually where the removable structure is introduced — so the finding carries the standard header line and its lane name. The re-review skip condition above depends on that header being present to find.
- One finding is the ordinary outcome, and silence is the expected outcome for most pull requests.
- Sketch a practical simpler direction and make clear the suggestion is advisory and for human decision. Do not use blocking language, merge-readiness claims, or correctness verdicts.
