---
title: Documentation Conventions
last-updated: 2026-07-30
---

# Documentation Conventions

How to write and maintain documentation in this repository.

## Doc Types

| Directory | Nature | What it answers | Update expectation |
|-----------|--------|----------------|-------------------|
| `docs/architecture/` | Evergreen | "How does the adapter work?" | Update whenever the system changes |
| `docs/references/` | Evergreen | "How do I perform this task correctly?" | Update whenever the pattern changes |
| `docs/guides/` | Evergreen | "How do I set up or operate this workflow?" | Update whenever the workflow changes |
| `docs/design-docs/` | Point-in-time | "Why was this decision made?" | Depends on `status` |

`AGENTS.md` is the quick-reference and Documentation Map. Detailed conventions, examples, and extended rationale belong in the appropriate docs directory.

## Documentation Checks

`scripts/lint_docs.py` runs in CI and locally from anywhere in the repo:

```bash
python3 scripts/lint_docs.py
python3 scripts/lint_docs.py --fix
```

It checks:

| Check | Enforces |
|-------|----------|
| `freshness` | A changed doc's `last-updated:` is a real current date |
| `links` | Inline markdown link targets resolve |
| `anchors` | `#fragment` targets resolve |
| `map_paths` | Documentation Map paths and `superseded-by:` targets resolve; design docs are mapped |
| `map_status_section` | A design doc's map section matches its status |
| `map_cells` | Consult-when cells remain one line and under the configured limit |
| `frontmatter` | Required fields are present, non-empty, and valid |

Only freshness is diff-scoped. The structural checks run repository-wide because moving one doc can break inbound links and map rows outside the diff.

## Documentation Maps

Every doc is reachable through the map in `AGENTS.md`. Add or update its row in the same PR as the document.

Use these sections in order:

| Section | Holds |
|---------|-------|
| `Architecture` | `docs/architecture/` |
| `References` | `docs/references/` |
| `Guides` | `docs/guides/` |
| `Active Design Docs` | `status: proposed` or `status: active` |
| `Historical & Deprecated Design Docs` | `status: completed` or `status: deprecated` |

Put this sentence immediately above the Historical table:

> Decision records, not descriptions of the running system — consult them for *why* something was chosen, never for how it works today.

A design doc's status selects its section. Moving from `active` to `completed` moves the row; moving from `completed` to `deprecated` leaves it under Historical. Do not repeat the status with `(deprecated)` or `Historical:` labels in the row.

The Consult-when cell is a routing trigger, not a summary. Keep it to one line and name the situations or concepts that should cause an agent to open the doc.

## Required Frontmatter

Architecture, reference, and guide docs require:

```yaml
---
title: Human-readable title
last-updated: 2026-07-29
---
```

Design docs require:

```yaml
---
title: Human-readable title
status: active
created: 2026-07-29
last-updated: 2026-07-29
---
```

Valid design statuses:

- `proposed` — written but not yet accepted or started;
- `active` — authoritative plan while work is in flight;
- `completed` — implemented decision record whose body is frozen;
- `deprecated` — retired record that no longer describes the current system.

Add `superseded-by:` when a deprecated doc has an evergreen or newer design-doc successor. It is optional when no successor exists.

## Writing Prescriptive Conventions

Keep codebase-specific facts and traps that a capable contributor could reasonably miss. Do not pad the doc with generic framework, language, HTTP, SQL, or testing tutorials.

- Explain a non-obvious rationale once and point to it elsewhere.
- Scope absolute claims by checking the full repository for counterexamples.
- Do not restate call-site counts, tunable constants, dependency inventories, or line numbers; point to the owning file, symbol, or grep.
- Keep implementation mechanics in code. A reference doc states the durable pattern and cites the implementation — and when the rule is newer than the code, re-check every cited implementation still obeys it, or name the one that predates it. A citation that violates the rule stated beside it teaches the antipattern.
- Verify third-party and upstream-Immich behavior from primary sources or the checked-out upstream source.
- Everything committed here must stand on its own for a public reader. Describe the contract and rationale instead of relying on a private ticket or sibling path; follow `code-practices.md` § Project Conventions.

If a reference doc grows because it covers several legitimate topics, split it behind a short index instead of deleting high-signal rules.

## Design Doc Lifecycle

### Keeping Active Design Docs Consistent When Shipping

An active design doc is the authoritative plan. Update it in the same PR when implementation changes a decision. When work fully ships, set `status: completed` and move its map row to Historical.

### Evolution Notes for Completed Design Docs

Do not rewrite the body. Append a dated `## Evolution Notes` entry when the running system moves past the decision, stating what changed and pointing to current code or an evergreen doc.

### Retire Design Docs; Don't Maintain Them

Retire a completed doc when an evergreen doc owns the live subject, the body would need repeated evolution notes, functionality was removed, or the decision was reversed.

### Reclassifying a Completed Doc to Deprecated

1. Move still-current architecture or operational guidance into an evergreen doc.
2. Set `status: deprecated` and add `superseded-by:` when applicable.
3. Add a banner after the H1 pointing to the live source.
4. Preserve the historical decision and rationale, but remove instructions that pretend to be current.
5. Keep the row under Historical and route its Consult-when cell toward the live source.

## Freshness

Bump `last-updated:` whenever a doc body changes meaningfully. Run `python3 scripts/lint_docs.py --fix` to update missed dates. A content-preserving rename does not require a bump; a renamed-and-edited doc does.

Do not leave a date-only bump after reverting the body edit that prompted it. Compare the final file against `main`.

## Path Citations

Use the shortest path that resolves unambiguously from the citing file. Same-directory markdown links may use a local filename; other in-repo links use a correct relative target. Cite source by file plus symbol, never by line number.

Do not cite a private sibling repository as the explanation for adapter behavior. State the public Gumnut API contract this repo relies on. When an external public source is useful, use a resolvable link.

## Verify Claims

Resolve every citation and check every named symbol, setting, CLI command, and upstream behavior before shipping. A task description and a correction are both claims to verify, not premises. Re-read source and documentation changed in the same PR against each other; each can look correct in isolation while contradicting the other.

## Evergreen Framing

Architecture, reference, and guide docs describe durable current knowledge. Remove intra-PR iteration, review-round narration, and "we just changed" framing. Keep the fact, rationale, and stable code anchor.
