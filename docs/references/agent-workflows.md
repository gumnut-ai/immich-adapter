---
title: Agent Workflow Design
last-updated: 2026-10-27
---

# Agent Workflow Design

Use this reference when authoring or reviewing agent workflows, daemon policies,
and their supporting scripts in this repository.

## Separate Judgment From Enforcement

The agent owns investigation: inventorying sources, invoking native audit and
resolver tools, inspecting their outputs, assessing compatibility and exposure,
and deciding what work is useful. Let it use existing tools directly rather
than building custom collectors, validators, or orchestration around those tasks.

Custom workflow code must enforce a boundary around the agent and must not
duplicate existing CI. Missing CI coverage alone does not justify a workflow
check; removing a quality gate does not require adding an equivalent CI gate.
Prefer native permissions, credential isolation, resource limits, and existing
configuration over custom implementations. Use native safe outputs for privileged publication. The agent reconciles
uncertain writes from actual GitHub state before any retry. Repetition or a desire for structured shared input alone
does not justify a wrapper.

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

## Native maintenance workflows

Dependency maintenance, librarian and maintainer compile directly with unmodified
GH-AW v0.89.21. Their source frontmatter defines read-only agent tools, native
budgets, serialized concurrency, default-branch admission, isolated App publication
and successful agent/detection gating. No maintenance helper patches the compiler,
runtime or generated YAML. Shared imports contain transport-neutral instructions.

Local role policy retains work caps, readiness, deduplication and human selection.
Numeric assignment/review targets are used on a later activation after exact-head
CI and known mergeability; code changed in that activation is never handed off.
Native publication cannot upload Git LFS objects. Defer those proposals. Ordinary
PR review may include workflow and security-file changes within the role's scope.
