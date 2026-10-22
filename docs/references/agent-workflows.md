---
title: Agent Workflow Design
last-updated: 2026-10-22
---

# Agent Workflow Design

Use this reference when authoring or reviewing agent workflows, daemon policies,
and their supporting scripts in this repository.

## Separate Judgment From Enforcement

The agent owns investigation: inventorying sources, invoking native audit and
resolver tools, inspecting their outputs, assessing compatibility and exposure,
and deciding what work is useful. Let it use existing tools directly rather
than building custom collectors, validators, or orchestration around those tasks.

Custom workflow code belongs only in guardrails around the agent that CI does
not already enforce. Prefer native permissions, credential isolation, resource
limits, and existing configuration over custom implementations. Keep privileged
publication and uncertain-write recovery outside the agent's control where
those boundaries require enforcement. Repetition or a desire for structured
shared input alone does not justify a wrapper.

## Validate the Proposal

Follow the repository's existing testing guidance and use its documented
commands. The agent chooses and runs appropriate local checks, recording
failures and unavailable checks accurately. Ordinary PR CI and independent
review assess the proposed change; workflow-specific scripts do not duplicate
checks CI already owns. Passing checks or publishing a PR does not establish
that removing coverage is justified or that a document is factually correct;
review those claims against the evidence.

Test observable behavior and shared contracts at the layer that owns them,
including meaningful failure cases. Existing caller or integration coverage
may establish a helper's behavior; adding a function does not automatically
require a separate unit test. Add tests for demonstrated gaps rather than to
mirror implementation structure.

Missing evidence withholds dependent claims or actions. Report the affected
scope and unknowns without treating an unrelated source failure as proof that
no useful work exists. Preserve verified findings in incomplete reports.
