---
title: "Immich Adapter Architecture"
last-updated: 2025-10-09
---

# Immich Adapter Architecture

## Overview

- Immich mobile/web app --> immich-adapter --> photos-api
- Immich mobile/web apps are either the original Immich apps, or lightly customized ones for Gumnut. Both options should ideally work perfectly. Gumnut may not support the latest version of the apps due to breaking changes.
- immich-adapter is a custom Python FastAPI backend that translates native Immich requests to Gumnut's API. Ideally, it would be stateless and all of the backend state would be in Gumnut, but we'll have to see how feasible that is. There might be Immich-specific settings, for example, that we would want to store in a database associated with this adapter. immich-adapter is open-source, and open to contributions from anyone that wants to help bridge Immich and Gumnut.

Notes on login flow:

- First user is admin: https://immich.app/docs/overview/quick-start
- There's no sign-up. Just a email/password login. How is it configured?
