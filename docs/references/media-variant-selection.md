---
title: "Media Variant Selection"
last-updated: 2026-10-01
---

# Media Variant Selection

## Thumbnail variant selection by aspect ratio

Use `routers/api/assets.py::_upgrade_variant_for_aspect` for thumbnail
selection; its docstring and `_LANDSCAPE_SMALL_ASPECT_THRESHOLD` comment own
the aspect-ratio rule, display-space assumptions, and bandwidth rationale.
Keep every upgrade target in both `AssetVariant` and `_VIDEO_IMAGE_VARIANTS`
so videos resolve to the `_image` key. The upgrade happens before the URL
existence check and assumes the upgraded rung exists whenever the thumbnail
does; if that backend guarantee changes, gate on the upgraded key's presence.
