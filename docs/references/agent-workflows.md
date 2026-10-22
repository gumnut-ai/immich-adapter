---
title: Agent Workflow Design
last-updated: 2026-10-22
---

# Agent Workflow Design

Use this reference when authoring or reviewing agent workflows, daemon policies,
and their supporting scripts in this repository.

## Separate Judgment From Enforcement

Assign investigation, prioritization, compatibility analysis, and evidence
interpretation to the agent. Scripts can inventory sources, call native audit
or resolver tools, validate their outputs, and enforce objective limits.
Keep relevant facts available for agent investigation instead of treating a
heuristic as a final decision about semantic eligibility. An AST can count
removed tests; deciding whether their coverage is obsolete needs evidence and
judgment.

Keep credential isolation, permissions, scope limits, objective supply-chain
rules, budgets, publication identity, and uncertain-write recovery mechanical.
Use existing configuration and standard tools where they already own a rule.
This division preserves safeguards while avoiding a parallel script
implementation of the agent's reasoning.

## Validate the Proposal

Honor required local checks and reuse standard project commands rather than
building a second workflow-specific implementation of the full CI matrix.
Ordinary PR CI and independent review assess the proposed change. Passing
checks or successfully publishing a PR does not establish that removing
coverage is justified or that a document is factually correct; review those
claims against the evidence.

Test observable behavior and shared contracts at the layer that owns them,
including meaningful failure cases. Existing caller or integration coverage
may establish a helper's behavior; adding a function does not automatically
require a separate unit test. Add tests for demonstrated gaps rather than to
mirror implementation structure.

Missing evidence withholds dependent claims or actions. Report the affected
scope and unknowns without treating an unrelated source failure as proof that
no useful work exists. Preserve verified findings in incomplete reports.
