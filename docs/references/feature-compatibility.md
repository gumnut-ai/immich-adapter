---
title: "Feature Compatibility"
last-updated: 2026-09-02
---

# Feature Compatibility

This reference records the adapter's compatibility policy for feature areas. It
classifies current support and known differences without serving as an endpoint
catalog or a roadmap. The running code, generated Immich models, and feature
flags own exact routes, schemas, response shapes, and reachability.

## Classifications

### Supported/current

Core client workflows are supported where the adapter has a faithful Gumnut API
translation: uploads, timeline and albums, people and faces, search, map
markers, stacks, trash, direct video playback, the implemented asset edits, and
mobile sync. The [adapter architecture](../architecture/adapter-architecture.md)
describes the current boundaries, translations, and failure behavior.

### Product-dependent

Sharing is product-dependent. Shared links, partner and album sharing, and
sharing-dependent activity or comment surfaces require a durable access model
and cross-user authorization that cannot be supplied by an adapter-only
translation. They remain outside current support until that product capability
exists.

### Deferred compatibility gaps (non-commitments)

Known compatibility differences without an active owner or approved scope are
deferred compatibility gaps, not commitments or a priority order. This includes
tags, reverse geocoding, persistent memories writes, specialized search and
trash-aware search limitations, API-key management, custom metadata, remaining
asset edits and OCR, folder view, large-scale pagination, and stack-aware month
counts. A gap stays in this classification until its product and implementation
context changes; its presence does not promise future delivery.

### Intentional unsupported areas

Some Immich surfaces are outside the adapter's product boundary or conflict with
the single-user Gumnut library model. These include adaptive video streaming,
integrity and database-maintenance workflows, OAuth backchannel logout, library
management, session lock/PIN, administration and user management, unsupported
notification and job/queue infrastructure, and plugins/workflows. Duplicate
detection follows Gumnut's different product approach. The calendar heatmap is
a benign empty compatibility read.

Some legacy mutation stubs still return compatibility success without durable
storage. That response keeps a client usable; it does not make the feature
supported. New or revised compatibility behavior must not claim durable success
without persistence. Changing an existing stub's client-visible response
requires separate behavioral review.

## Keeping classifications current

When a feature changes, classify the feature area here and then verify the
route, generated DTO, client preference, and server-feature behavior in code.
Do not copy those mutable details into this reference. Re-check tagged upstream
Immich source for client reachability and semantics when the pinned version
changes; version coordination and model-generation rules are in [Routes, DTOs,
and Upstream Compatibility](routes-dtos-and-upstream-compatibility.md).
