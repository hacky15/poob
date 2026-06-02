---
type: decision
status: active
date: 2026-06-02
tags: [scanner, reliability, observability, heartbeat, alerting]
related: [[watchlist-sweep-unprotected-cdp-wedge]] [[main-browser-cdp-wedge-infinite-hang]] [[fb-session-cookie-persistence]]
---

# Proactive health heartbeat — never let the scanner go dark unnoticed

## Context

Across this saga the scanner went fully dark (zero deal notifications) repeatedly, each time for *hours*, and **nobody knew until the operator complained.** The 2026-06-02 case ran 15.5h. Every signal needed to detect it was LOGGED but watched by nothing, and the only owner-facing ping was a `"Poob is alive 🍑"` startup DM that fired 6s *after* `status=failed`. The operator's demand: stop getting blindsided — make a dark-out impossible to miss.

Two hard constraints shape the design:
- Recovery is `os._exit(1)` (a hard kill), so an in-process alert task **cannot be relied on to fire**. The signal must be written to durable storage *before* exit and surfaced on the *next* boot.
- The one failure the bot **cannot self-heal** is `auth=failed` (needs an operator cookie re-seed). That alert must be actionable and runbook-linked — not a generic "something's wrong."

## Decision

Emit durable health signals from the scanner and DM the owner on the dark-out fingerprints, with transition de-dup so alerts never become the new ignored "alive" ping.

- **`src/poob/scanner/health_monitor.py`** (new) — pure decision logic + record helpers over `PersistentKV` (survives `os._exit`):
  - `record_force_exit(kv, reason)` — called before each `os._exit` (watchdog + memory guard).
  - `record_delivery(kv)` — called on each successful deal notification.
  - `pending_health_alerts(kv, auth_ok, now, ...)` — returns owner-alert strings for: recovered-from-forced-restart (next-boot), `auth=failed` (loudest, runbook-linked, can't-self-heal), and long delivery-silence (coarse backstop). Transition-de-duped via KV flags (alert once on entry, once on recovery).
- **Wiring:** `patrol_scheduler` records the force-exit reason before both `os._exit` sites; `patrol_engine` records delivery on both send paths; `main.py` runs a thin heartbeat loop that waits for the bot to connect and DMs the owner via the bot client.
- **Cross-workstream boundary:** the DM *sink* (the bot client, `on_ready`, the misleading startup DM) lives in `src/poob/discord_bot/` (voice workstream). This change does **not** edit `bot.py` — `main.py` supplies the delivery using the bot client. A `docs/_inbox/` note hands off the deeper Discord-side work (generalize `_notify_owner_alive` → `dm_owner`; make the startup DM report auth/last-delivery instead of unconditional "alive").

## Why this shape (oversights it avoids)

- **os._exit kills in-process alerts** → durable KV + next-boot surfacing, not a best-effort pre-exit DM (the scheduler holds no bot reference anyway).
- **Alert on the OUTCOME, not just the crash** → most of the 15.5h had no force-exit; auth-failed + delivery-silence are first-class conditions.
- **Transition de-dup** → no per-cycle spam that trains the operator to mute it.
- **Auth-failed is the actionable one** → its alert names the cookie-re-seed runbook.

## Follow-ups (fast-follow, now heartbeat-protected)

- **Precise dark-out fingerprint:** alert on `notify.skip_too_old score=incredible` (a deal QUALIFIED but was age-gated = structural dark, near-zero false positives) — higher precision than the coarse no-delivery-in-N-hours backstop, which can false-positive on genuinely quiet nights.
- **Derived auth signal:** drive the auth-failed alert off a per-cycle live signal (auth-sweep-timeout / `auth_count==0` streak), not the set-once `is_authenticated` boot flag, so mid-run session death (no restart) is caught instantly — pairs with the per-cycle auth re-validation (fix A).
- **Shadow-ban alert:** surface `shadow_ban=True` (the independent GQL rate-limit dark driver).
- **Discord-side (cross-workstream, docs/_inbox):** honest startup DM + generalized `dm_owner`.

## Validation

- `tests/unit/test_health_monitor.py`: force-exit recovery alerts once then clears; stale force-exit ignored; auth-failed alerts once then recovery; dark-out after long silence (de-duped); recent delivery silent; auth-failed owns the dark-out (no double-alert); healthy state silent.
- Prod: after deploy, a watchdog/memory restart should produce an owner DM on next boot, and an `auth=failed` startup should DM the cookie-re-seed runbook within one heartbeat interval.
