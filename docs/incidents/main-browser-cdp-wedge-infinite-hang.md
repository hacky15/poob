---
type: incident
status: resolved
date: 2026-05-31
tags: [scanner, patrol, browser, cdp, reliability, watchdog, outage]
related: [[anon-browser-cdp-death-no-recovery]] [[enrichment-no-time-budget-cycle-abandonment]] [[authenticated-discovery-sweep]]
---

# Main-browser CDP wedge → every cycle hangs 600s → ~18.5h of zero notifications

## Symptom

The user reported "no notification in nearly a day." Prod check (2026-05-31 17:46Z): container **up** (no crash, no OOM, mem 2 GB), but **every patrol cycle for ~18.5h had hung at the 600s ceiling and been abandoned** — zero enrichment, zero evaluation, zero deals, zero notifications.

- Last successful cycle: `2026-05-30T23:09Z`. Then every cycle abandoned.
- `Authenticated browser DOM sweep` log occurrences in a 3h window: **0** (the sweep never even started logging).
- 12 `Patrol cycle hung past 600s — abandoned` in 3h.
- A representative cycle: `Category sweep complete` (anon DOM) at T+11s, then **590s of total silence**, then the 600s abandon. The hang is in the phase right after the anon DOM sweep — the authenticated main-browser sweep entry (`_browser.get_page()`).

## Root cause — two compounding failures

**1. The main (authenticated) browser's CDP session wedged** (~4h after the container's 19:05 start), the same "CDP death" pattern documented for the anon browser in [[anon-browser-cdp-death-no-recovery]] — but on the *main* browser, which has no in-process self-heal.

**2. The decisive one: `asyncio.wait_for` cannot cancel a wedged CDP call.** The authenticated sweep was *already* wrapped in `asyncio.wait_for(self._auth_dom_sweep(result), timeout=60.0)` — yet it hung ~590s and never logged its 60s timeout. A wedged `cdp_use` `get_page()` blocks in a way that does not honor cancellation, so `wait_for` waits indefinitely for a cancellation that never completes. **In-cycle timeouts are therefore insufficient against this class of wedge.** The scheduler's outer 600s `wait_for` did eventually abandon the cycle — but the abandon path does **nothing to recover the browser**, so every subsequent cycle re-entered the same wedged `get_page()` and hung again. Unbounded.

## Fix — process-restart reliability watchdog (the only thing that works against a non-cancellable wedge)

Added a consecutive-failure watchdog to `PatrolScheduler` ([patrol_scheduler.py](../../src/poob/scanner/patrol_scheduler.py)): `_record_cycle_outcome(succeeded=)` is called on every cycle outcome. A success resets the counter; a failure (hang-abandon **or** error) increments it, and at `patrol_max_consecutive_failures` (default **3**) consecutive failures the scheduler logs CRITICAL and calls `os._exit(1)`. The container's `RestartPolicy=unless-stopped` then brings up a **fresh** process — new browser sessions, fresh cookie import, re-auth. This bounds worst-case zero-output time to **N × the cycle timeout (~30 min)** instead of unbounded (was 18.5h).

This is deliberately failure-mode-agnostic: it does not try to diagnose or cancel the wedge (which is impossible from inside the same process). Whatever wedges — main-browser CDP, anon-browser CDP, a future unknown deadlock — repeated abandons trip the watchdog and a fresh process recovers. It is the same recovery a manual `docker restart` performs, automated.

Immediate recovery was a manual `docker restart poob` (17:48Z); cycles resumed (anon-only — see Follow-ups).

### Why NOT more in-cycle timeouts / in-process browser self-heal

A wedged `get_page()` ignores `asyncio.wait_for` cancellation, so adding more wrapping timeouts (or a main-browser `_restart` whose `stop()` would itself block on the wedged session) is **not reliable**. Those approaches were considered and rejected here in favor of the process-restart backstop, which has no such dependency. The existing 60s auth-sweep `wait_for` is retained (it catches *cancellable* slow sweeps) but is not the guarantee.

## Validation

- `tests/unit/test_patrol_scheduler.py::TestReliabilityWatchdog`: N consecutive failures → `os._exit(1)`; a success resets the counter; explicit/defaulted threshold from config.
- Full `test_patrol_scheduler.py` (23) green.
- Prod: after deploy, watch for `Patrol failed consecutively ... Force-exiting` only under genuine wedges, and confirm the container auto-restarts and resumes completing cycles.

## Follow-ups

- **Cookie expiry (separate, now due):** after the manual restart, `FB authentication result status=failed` — the imported FB session cookies expired in the ~19h. The auth sweep is correctly skipped while unauthenticated (so it no longer contributes to the hang), but the *fresher* authenticated feed is offline until cookies are re-imported (runbook: [[facebook-cookie-import]]). The deferred "session-health re-auth" follow-up is now operationally due.
- **Reduce wedge frequency at the source:** the chromium process/memory leak that degrades CDP (see the stability audit + [[anon-browser-cdp-death-no-recovery]] follow-ups) should be contained (PGID reaping + memory-pressure guard) so the watchdog rarely needs to fire.
- **Consider** a complementary "no successful cycle in X minutes" watchdog for silent (non-abandoning) stalls; the consecutive-abandon counter covers the observed failure.
