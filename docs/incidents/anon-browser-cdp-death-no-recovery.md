---
type: incident
status: resolved
date: 2026-05-29
tags: [scanner, browser, cdp, anonymous-browser, ingestion, notifications]
related: [[husqvarna-enrichment-cross-contamination]] [[listing-freshness-verification]]
---

# Anonymous browser CDP death with no in-process recovery → 3 days of zero ingestion

## Symptom

User reported the homelab server "running hot for a long time but no notifications … the last many days." Two distinct claims, both correct:

1. **The heat was not poob.** `docker stats` showed poob at **1.02% CPU** / 1.6 GiB. Host top consumers were `containerd` (26%) + `dockerd` (24%) + a `mongod` (another project — poob uses SQLite). The scanner was nearly idle.
2. **No notifications for days.** Only 2 deals ever recorded in the `deals` table (2026-05-24 21:26, 2026-05-26 00:45). It was 2026-05-29. `scan_logs` showed `deals_found=0` across every hour bucket for 12h+.

The scraper *was* running — 4,236 scan cycles since April 23, scheduler firing every ~5 min — but ingesting **zero listings**: `source=no_data total_seen=0` every cycle.

## Root cause

Both ingestion tiers were dead simultaneously:

- **GraphQL tier**: Facebook hard-rate-limited the datacenter IP. `graphql_client.rate_limit_circuit_breaker consecutive_hits=138`, cooldown pinned at 3600s, re-tripping on every probe. This is FB-side and time-only-heals.
- **DOM browser tier (the real bug)**: the anonymous browser's CDP session died and **never recovered in-process**. Timeline from logs:
  - 2026-05-26 02:06 — container restart, browser starts clean
  - 2026-05-26 02:17–18:37 — DOM sweeps healthy (`count=18`)
  - 2026-05-26 **18:57** — first `Anonymous browser DOM sweep timed out (60s)`
  - 2026-05-26 18:57 → 2026-05-29 15:44 — **524 consecutive 60s timeouts**, zero recovery

The 60s per-step timeout (added earlier to stop a hung CDP session from eating the whole patrol cycle) bounded each cycle correctly — but nothing ever *fixed* the degraded session. Once the anon browser's CDP connection dropped (Discord WS reconnect, chromium crash, or slow resource leak — the ~16h-to-death pattern fits a gradual leak), every subsequent `get_page()` / navigation blocked until the 60s timeout fired, forever, until a manual `docker restart`.

This had been band-aided twice in the prior session with container restarts. The restarts restored service for ~16h each, then it died again. The restart was treating the symptom; the missing piece was in-process recovery.

## Fix

Added an in-process self-heal in `PatrolEngine` (`src/poob/scanner/patrol_engine.py`):

1. **`_anon_sweep_consecutive_timeouts`** counter, incremented on each `asyncio.TimeoutError` from the 60s-bounded `_anon_dom_sweep`, **reset to 0 whenever a sweep completes** (browser responsive, even if it found zero listings — that distinguishes "hung" from "fine but empty feed").
2. After **`_anon_sweep_max_timeouts`** consecutive timeouts (default 3, ~15 min, config `patrol_anon_browser_max_timeouts`), call **`_restart_anonymous_browser()`**.
3. **`_restart_anonymous_browser()`** does `await anonymous_browser.stop()` (bounded by `asyncio.wait_for(timeout=30)`) then `await anonymous_browser.start()` (bounded 90s). `BrowserManager.start()` assigns a *fresh* `BrowserSession`, so even a `stop()` that itself hangs is recovered by the subsequent `start()`. On success the counter resets; on `start()` failure the counter stays elevated so the next cycle retries rather than giving up.

This is the anon-browser analogue of the main browser's `_cdp_permanently_broken` short-circuit — except instead of giving up, the anon browser (our primary DOM tier) actively recreates itself, because losing it means losing ingestion entirely.

## Validation

- 6 new unit tests (`TestAnonBrowserSelfHeal`): below-threshold counting, restart-at-threshold, success resets counter, restart helper stop→start ordering, start-even-if-stop-hangs, counter-stays-elevated-on-start-failure. All pass.
- Full `test_patrol_engine.py` suite green (no regressions).
- Post-deploy: watch for `Recreating anonymous browser after consecutive sweep timeouts` followed by `Anonymous browser recreated successfully` and a return of `Anonymous browser DOM sweep count=N` (N>0). `source=no_data` runs should stop being permanent.

## Follow-ups

- **Zombie chromium processes.** `stop()` may not cleanly reap the chromium child if the session is wedged. Repeated recreations could leak processes and approach the 4 GiB container cap (currently ~1.6 GiB). The self-heal only fires after 3 consecutive timeouts and resets once healthy, so steady-state thrash is unlikely — but if memory climbs across many heals, add an explicit chromium-process kill in the restart path. The container image lacks `ps`, so process inspection needs `/proc` walking or a pkill in the entrypoint.
- **GraphQL IP rate-limit** remains unaddressed at the root — FB has hard-limited the datacenter IP. Real fix is a residential-proxy / browser-identity-pool rotation (deferred scanner feature). Until then the DOM tier (now self-healing) is the dependable path.
- **Notification volume is still gated by FB feed freshness** even with healthy ingestion — see [[husqvarna-enrichment-cross-contamination]] and the triage-freshness decisions. This incident only restores *ingestion*; it does not change how rarely a truly-fresh INCREDIBLE local deal appears.
