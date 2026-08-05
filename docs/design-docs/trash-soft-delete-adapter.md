---
title: "Trash: Soft-Delete with Retention (Adapter)"
status: deprecated
superseded-by: ../architecture/adapter-architecture.md
created: 2026-04-20
last-updated: 2026-08-05
---

# Trash: Soft-Delete with Retention (Adapter)

> **Deprecated —** This document records the adapter-side decisions behind
> Immich-compatible trash and permanent deletion. It was pruned on 2026-08-05
> to the context, cross-service contract, and evolution notes; endpoint,
> filtering, event, and configuration inventories are owned by the code. For
> current behavior, see [`adapter-architecture.md`](../architecture/adapter-architecture.md)
> under “Trash and Deletion Semantics.”

## Context

The adapter now implements Immich's trash flow on top of the Gumnut API soft-delete primitives. Immich clients still call the native `DELETE /api/assets` endpoint with the `force` flag plus the existing `/api/trash/*` routes; the adapter translates those calls into backend trash, restore, and permanent-delete operations without changing the public wire contract.

This work spans delete semantics, trash endpoints, timeline/statistics filters, sync `deletedAt` propagation, WebSocket events, and the `trashDays` value shown in the web app. This doc records the shipped adapter behavior and the remaining deliberate limitations.

## Implemented outcome

The adapter preserved Immich's public trash contract while translating it onto
the Gumnut API's soft-delete, restore, and permanent-delete primitives. The
`force` flag distinguishes trash from permanent deletion; trash-aware reads and
sync/realtime events keep client state coherent; and restore-all and empty-trash
enumerate the target set before mutation so shrinking result sets do not break
pagination. The adapter and API share the configured retention window.

## Dependencies

This adapter behavior relies on the following backend capabilities:

- asset-level trash state via `trashed_at`
- bulk trash/restore/permanent-delete endpoints
- asset listing/counting with `state="trashed"` and `state="all"`
- distinct trash, restore, and permanent-delete asset events for the sync stream and realtime channels
- a shared `TRASH_RETENTION_DAYS` deployment contract

## Evolution Notes

- **2026-07-20**: Moved trash mutation chunking to the shared `GUMNUT_API_MAX_BULK_IDS` contract so trash, album, asset-update, and sync hydration calls follow the same Gumnut API bulk limit.
