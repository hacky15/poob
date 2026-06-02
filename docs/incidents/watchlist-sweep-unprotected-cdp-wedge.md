---
type: incident
status: resolved
date: 2026-06-02
tags: [scanner, patrol, browser, cdp, auth, cookies, reliability, outage]
related: [[main-browser-cdp-wedge-infinite-hang]] [[fb-session-cookie-persistence]] [[authenticated-session-no-human]]
---

# 15.5h dark-out: CDP wedge → frozen cookie token → auth death loop

## Symptom

Operator: "haven't seen anything in a while." Last delivered deal `2026-06-01T23:55:08Z`; **15.5h of zero notifications**. Container up, restarted twice (watchdog force-exits ~7h apart: 07:10:42Z, 14:12:24Z), `auth=failed` after each restart.

## Root cause — a closed, self-perpetuating loop (4 SPOFs + 1 observability hole)

An exhaustive, adversarially-verified diagnosis showed this was never N separate bugs — it's one loop, and each prior fix closed one link while the loop stayed intact:

1. **The main browser's CDP session ages into a wedge (~every 7h)** and the wedge lands on the **one unprotected CDP path**: the watchlist DOM fallback `_sweep_watchlist_items → sweep_search → navigate_and_wait` (no `asyncio.wait_for`). The auth/anon DOM sweeps were 60s-bounded and enrichment 20s-bounded — this path was not. A wedged call ignores cancellation, so the cycle hangs the full 600s → 3 abandons → watchdog `os._exit`.
2. **Cookie persistence ran only at cycle-END** (`run_patrol_cycle` tail) — unreachable on an abandoned/wedged cycle.
3. **`persist_cookies` silently `return 0` when the live read lacked `xs`** — so a structural capture failure looked identical to "not yet due." `cookies.json` froze at `00:15:56Z` and never refreshed.
4. **`is_authenticated` is a set-once boot flag** (`mark_authenticated` only at startup), so the engine kept trusting it while the live session was already dead.
5. **Only owner signal was a "Poob is alive 🍑" startup DM** — which fired 6s *after* `status=failed` both restarts. No one was paged for 15.5h.

Loop: wedge → restart → re-import the **frozen stale** `cookies.json` → FB already rolled the token → `auth=failed` → fresh "just-listed" feed dark → every anon deal age-gated out (`notify.skip_too_old score=incredible`) → silence. **Recovery re-introduced the dead state.**

## Fix (this note covers the first two layers; see follow-ups)

- **Bound the unprotected path** (layer 2 — contain): the watchlist DOM sweep is now wrapped in `patrol_watchlist_dom_timeout_s` (60s) and only runs when `is_authenticated`, with a consecutive-CDP-failure counter feeding the main-browser self-heal. ([patrol_engine.py](../../src/poob/scanner/patrol_engine.py) watchlist DOM fallback.)
- **Capture the rolled token where CDP is provably alive** (foundational): persist now fires right after a *successful* auth sweep — early in the cycle, before the wedge-prone enrichment phase — not only at cycle-end. And `persist_cookies` now **logs the `xs`-absent skip** ([manager.py](../../src/poob/browser/manager.py)) instead of returning 0 silently, so a capture failure is visible (and is itself a session-death signal). This keeps `cookies.json` ≤ one interval fresh, so a watchdog restart re-imports a *recent valid* token and auth **survives** the restart — breaking the loop.

## Follow-ups (defense-in-depth, in progress — heartbeat-protected)

- **A — per-cycle auth re-validation:** replace the set-once flag with a throttled live session probe; re-import on death (now succeeds because the token is kept fresh). Kills SPOF #4.
- **C — preemptive main-browser recycle:** recycle the main browser on a ~4h timer (below the ~7h wedge age), between cycles, re-importing+persisting cookies — so CDP never reaches wedge age. Replaces the dead-end `_cdp_permanently_broken` give-up with a real self-heal.
- **D — heartbeat (the no-more-blindness deliverable):** durable PersistentKV health state (survives `os._exit`) + owner DM on next boot / `auth=failed` / age-gated-INCREDIBLE dark-out fingerprint. The honest answer to "we keep getting blindsided." DM delivery lives in `discord_bot/` (cross-workstream — see docs/_inbox/).
- **Independent:** the anon GQL rate-limit/shadow-ban death-spiral is a separate dark driver needing the deferred identity-pool/proxy ($); the heartbeat must alert on `shadow_ban=True`.

## Validation

- Existing `test_browser_manager.py` (persist), `test_patrol_engine.py` (session/budget) green; new init defaults covered.
- Prod: watchlist DOM timeouts should log instead of hanging 600s; `Persisted live FB session cookies` should appear ~every interval on healthy cycles (not freeze); `Cookie persist skipped — no 'xs'` is now visible when capture fails.
