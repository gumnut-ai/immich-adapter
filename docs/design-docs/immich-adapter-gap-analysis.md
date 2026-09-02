---
title: "Immich Adapter Gap Analysis"
status: deprecated
superseded-by: ../references/feature-compatibility.md
created: 2026-04-15
last-updated: 2026-09-02
---

# Immich Adapter Gap Analysis

> **Deprecated (2026-09-02):** This survey mixed historical compatibility
> evaluation with a mutable gap list and priority order. It no longer owns
> current feature classification. See [Feature Compatibility](../references/feature-compatibility.md)
> for the living policy; the content below is preserved for its historical
> evaluation rules and rationale.

## Context

At the time of this analysis, the adapter's runtime Immich pin in
`.immich-container-tag` was **v3.1.0**. The generated model surface at the
assessed head was generated from **v3.2.0-rc.2**. Core upload, timeline,
albums, people/faces, search, map markers, stacks, trash, video playback, the
Immich web editor's crop/rotate/mirror edits over Gumnut version chains, and
mobile sync workflows were implemented. The remaining differences were
concentrated in features whose product model did not yet exist in the Gumnut
API, adapter-only compatibility surfaces, and scaling limits caused by protocol
mismatch.

This was a prioritization record, not an endpoint catalog. Generated models and
the running FastAPI application own the exact route/schema surface. Re-run
`tools/validate_api_compatibility.py` for a new target rather than updating
copied counts here.

## Evaluation rules

Each gap is evaluated on:

- whether current Immich web or mobile clients reach it;
- whether a benign empty read is honest or a fake mutation would mislead;
- whether the Gumnut API has the required durable model;
- whether the adapter can translate the behavior without introducing a second source of truth;
- user value relative to implementation and operating cost.

A route being present in OpenAPI does not prove a client uses it. Check the pinned upstream source and feature gates before prioritizing it.

## Immich v3 feature-area decisions

The v3 retarget introduced feature families whose reachability was checked against the pinned clients:

- **Adaptive video streaming:** intentional gap. The adapter reports realtime transcoding disabled, so clients use direct video playback.
- **Integrity checks:** intentional gap. These are storage-administration workflows, not client photo workflows.
- **OAuth backchannel logout:** intentional gap. The adapter is not the OIDC relying party; user logout is served by the normal auth route.
- **Plugins/workflows:** intentional gap. The optional utility page is unsupported.
- **Calendar heatmap:** the user read is a benign empty compatibility response; the admin variant remains unsupported.
- **Album map markers:** closed. Album views use the implemented Gumnut coordinate query.

These decisions are product/architecture choices, not promises that every unsupported route must remain a stub forever. Reassess when a client feature gate or Gumnut capability changes.

## Stub behavior

The survey treated a benign empty read as honest when it cleanly rendered an
unsupported feature, while recognizing that a successful mutation response
could mislead when nothing was stored. It also treated client feature flags as
the first defense against exposing unsupported UI. These criteria informed the
classifications but did not establish that every legacy stub already followed
them.

Current policy, including how to handle legacy compatibility responses, lives
in [Feature Compatibility](../references/feature-compatibility.md). Changing an
existing client-visible response remains a separate behavioral decision.

## Outcome

**2026-09-02:** The gap survey was retired after its mutable live table and
priority order were replaced by the narrower evergreen [Feature Compatibility](../references/feature-compatibility.md)
classification. The historical v3 feature decisions, evaluation rules, and
stub policy remain here; route and model details remain in code and the
generated model surface, while current translation behavior is documented in
the architecture and compatibility references.
