---
type: decision
status: active
date: 2026-06-03
tags: [scanner, browser, memory, reliability, chromium]
supersedes: []
related: [[main-browser-cdp-wedge-infinite-hang]] [[anon-browser-cdp-death-no-recovery]] [[proactive-health-heartbeat]]
---

# In-process browser recycle (reclaim chromium memory without a whole-process restart)

## Context

browser-use's Chromium leaks renderer memory over hours — audit 2026-05-30
measured ~5.6 GB across ~22 processes climbing toward the 4 GiB container cap.
The existing mitigation (`patrol_memory_restart_pct = 0.85`,
`_maybe_restart_on_memory_pressure`) calls `os._exit(1)` when memory crosses
85% of the cgroup limit and lets the container restart policy bring up a fresh
process. That works as an anti-OOM backstop, but it is the wrong primary tool:

- It restarts the **whole process** — dropping the live Discord voice session,
  the gateway connection, and the in-flight patrol cycle — to reclaim memory
  that is almost entirely held by **Chromium**.
- The operator observed it firing ~every 5–6h (two owner DMs in ~20h on
  2026-06-03: "auto-recovered from a forced restart (memory-pressure restart)"),
  experienced as random restarts.

The memory guard's own comment named the constraint: "browser-use spawns
chromium internally, so we cannot safely reap individual processes without
risking the live browser." True for *individual* processes — but a clean
browser `stop()` kills the **entire** Chromium process tree (leaked renderers
included), and `start()` respawns fresh. That is exactly what
`_restart_anonymous_browser` already does for the anon browser on CDP wedge
([[anon-browser-cdp-death-no-recovery]]); the main (authenticated) browser had
no equivalent and was never recycled on any trigger.

`patrol_main_browser_max_age_s = 14400` (4h) was added during the dark-out saga
with the intent to recycle the main browser preemptively, but the recycle action
itself was deferred — the config tracked age with nothing wired to it.

## Decision

Reclaim leaked Chromium memory **in-process, between cycles**, before the
whole-process guard ever fires.

1. **`BrowserManager.restart()`** — bounded `stop()`+`start()` reusing the
   remembered `cookies_file`. Because the main browser uses a persistent
   `user_data_dir` (`browser_profiles/facebook/`), cookies/login survive the
   recycle. Bounds mirror the anon path (stop ≤30s, start ≤90s) so a wedged
   `stop()` can't block the recreate.
2. **`PatrolEngine.maybe_recycle_browsers(memory_frac)`** — called by the
   scheduler each loop, *before* `_maybe_restart_on_memory_pressure()`. Recycles
   the main browser (then re-runs `ensure_logged_in` to re-confirm
   `is_authenticated` — cheap now that the boot-render race is fixed,
   [[fb-auth-boot-render-race]]) and the anon browser when **either**:
   - container memory ≥ `patrol_memory_recycle_pct` (**0.70**, a soft floor well
     below the 0.85 os._exit), **or**
   - the main browser has aged past `patrol_main_browser_max_age_s` (4h).
   Throttled by `patrol_browser_recycle_min_interval_s` (600s) so it can't thrash
   if memory stays high from a source a browser recycle can't reclaim.
3. **The `os._exit` guard stays** as a last-resort backstop — now only for
   non-browser memory growth, which a browser recycle genuinely can't fix.

Net: the random whole-process restarts stop. Memory is reclaimed on a
predictable cadence (age) or reactively (soft threshold) while Discord, voice,
and the process stay up.

## Alternatives considered

- **Keep only the os._exit guard.** Simplest, but it is the bandaid the operator
  flagged — restarting everything to free browser memory, dropping live voice.
- **Reap individual Chromium PIDs.** Fragile heuristic that risks killing the
  live browser (the guard comment's original objection). A whole-browser recycle
  sidesteps it — we own the BrowserSession lifecycle, not individual renderers.
- **Lower the os._exit threshold.** Makes the disruptive restarts *more* frequent,
  not fewer. Wrong direction.
- **Recycle only on age, not memory.** Age alone can't react to a faster-than-
  expected leak; memory alone can't pre-empt a wedge. Both triggers cover both.

## Consequences

- Most memory reclamation now happens silently in-process; the owner should stop
  seeing "memory-pressure restart" DMs except for genuine non-browser growth.
- Each recycle adds ~10–20s between two cycles (restart + re-auth) at most every
  `min_interval`; it runs between cycles so it never eats a cycle's budget.
- Re-auth after recycle depends on the persisted session + the boot-render-race
  fix; if cookies have expired it degrades to anonymous and the heartbeat
  ([[proactive-health-heartbeat]]) alerts, same as boot.
- Watch `Recycling browsers in-process to reclaim leaked chromium memory` in the
  logs and confirm memory sawtooths below the 0.85 line instead of hitting it.
