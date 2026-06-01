---
type: decision
status: active
date: 2026-06-01
tags: [scanner, facebook, authentication, cookies, reliability]
related: [[authenticated-session-no-human]] [[facebook-cookie-import]] [[main-browser-cdp-wedge-infinite-hang]]
---

# Persist Facebook's rolled session cookies so auth survives restarts

## Context

The authenticated patrol browser adopts a one-time operator cookie export ([[facebook-cookie-import]]). In prod the imported session **died in ~2–3 days**, forcing a manual re-import — not a viable long-term posture (the operator correctly pushed back: a real browser stays logged in for *weeks/months*).

Root cause, confirmed in code: **we never saved Facebook's rolled session.** Facebook rotates the session token (`xs`) as the browser is used — a normal browser silently keeps the rolled token, which is why it stays logged in. Our flow only ever *imported* a snapshot:
- `ensure_logged_in` (auth.py) imports `cookies.json` (the one-time export) when the live session isn't already valid.
- There was **no** code path that ever read the live/rolled cookies back to disk.
- The container restarts frequently — deploys, plus the reliability watchdog/memory-guard which `os._exit` (hard kill, so Chromium can't flush its profile).
- So every restart fell back to the **original** export. Once Facebook rolled past it and invalidated that token, auth returned `status=failed`, and the fresh "just-listed" feed went dark — the dominant cause of the deal drought (measured: 0 of 752 listings <10 min old over 24h, because that feed only exists when logged in).

## Decision

**Periodically capture the live (rolled) FB cookies back to the volume** so every restart reloads the freshest session, not the stale original — mirroring how a real browser maintains a rolling session.

- `BrowserManager.persist_cookies(path)` — reads all live cookies via CDP (`Storage.getCookies`, falling back to `Network.getAllCookies`), keeps the `*.facebook.com` ones, and atomically writes them to `cookies.json` in the import format that `parse_cookie_export` round-trips. **Guard:** only writes when a session cookie (`xs`) is present, so a logged-out read never clobbers a good file; read failures never clobber either.
- `PatrolEngine._maybe_persist_session()` — called once per cycle, throttled to `patrol_cookie_persist_interval_s` (default 1800s) and gated on `BrowserManager.is_authenticated`. The engine already drives the authenticated browser each cycle, so the rolled token is captured shortly after Facebook issues it.

Net: the imported session self-refreshes across restarts. The operator imports **once**; from then on it should last as long as Facebook keeps rolling it (weeks+), hands-off.

## Why not rely on the Chromium profile

The main browser uses a persistent `user_data_dir`, so in principle Chromium persists rolled cookies to its profile DB. But the reliability layer `os._exit`s on wedges/memory pressure — a hard kill gives Chromium no chance to flush — and deploys recreate the container. Explicit capture-and-write does not depend on graceful Chromium shutdown, so it is robust to the abrupt restarts the reliability design intentionally uses.

## Consequences / open question

- If Facebook *also* invalidates the session **server-side** (bot detection), persistence alone won't keep it alive and we'd need proxy/identity-pool work (deferred, $). The homelab is on a **residential** Madison IP with slow cadence, so server-side invalidation is the less likely cause — but it's the thing to watch. **Validation in prod: measure how many days/restarts the session survives after this ships.** If it now lasts weeks, persistence was the cause and the problem is solved; if it still dies in days despite a current `xs` on disk, the cause is server-side and a different fix is needed.
- The `facebook-cookie-import` runbook's "sessions last weeks; re-import when expired" is now the *fallback*, not the routine path.

## Validation

- `tests/unit/test_browser_manager.py::TestPersistCookies` — writes only FB cookies, round-trips through the real `parse_cookie_export`, skips when no `xs` (logged out), returns 0 when not started, never clobbers an existing file on read failure.
- `tests/unit/test_patrol_engine.py::TestSessionPersistence` — persists when authenticated + interval elapsed; skips when unauthenticated, throttled within the interval, or disabled (interval 0).
- Prod (post-seed import): expect a periodic `Persisted live FB session cookies count=N`, and `status=logged_in` to survive subsequent restarts/deploys.
