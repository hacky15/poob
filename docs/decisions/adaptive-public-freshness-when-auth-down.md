---
type: decision
status: active
date: 2026-06-02
tags: [scanner, notifications, freshness, auth, degradation]
related: [[triage-freshness-converge-with-notify]] [[authenticated-discovery-sweep]] [[watchlist-sweep-unprotected-cdp-wedge]]
supersedes: []
---

# Relax the public freshness gate when the authenticated feed is down

## Context

The public channel posts only INCREDIBLE deals "posted within `public_notification_max_age_minutes` (10)" — the "just-listed AND clearly worth something" bar the operator was emphatic about. **But the <10-min listings only exist on the authenticated "newest near you" feed.** When that feed is down (a dead/expired FB session — which recurred repeatedly), the only live source is the **anonymous** feed, which serves listings tens of minutes old (FB strips recency for logged-out viewers). With the strict 10-min bar, that means **zero public notifications while auth is down** — even though the pipeline keeps finding genuine INCREDIBLE deals.

Measured 2026-06-02 (auth down): a cycle found "Fabulous Finds in Old Port Costa," 66% off ($50/$150, INCREDIBLE), and correctly held it at `notify.skip_too_old age_minutes=43.4 limit_minutes=10.0`. The deal was real and good; it was just 43 min old because the anon feed had no fresher copy. The operator, tired of the recurring auth-feed fragility and the cookie-re-seed dependency, chose **graceful degradation over going dark**: some slightly-older deals beat none.

## Decision

Make the public freshness gate **adaptive on auth availability**:

- **Auth UP** (`BrowserManager.is_authenticated`): keep the strict `public_notification_max_age_minutes` (10) — the just-listed bar, since the fresh feed can satisfy it.
- **Auth DOWN**: relax to `public_notification_max_age_no_auth_minutes` (new, default **60**) — so the anon feed's INCREDIBLE deals reach the channel instead of nothing.

Implemented in `PatrolEngine._notify` ([patrol_engine.py](../../src/poob/scanner/patrol_engine.py)): the public cutoff is chosen from `is_authenticated` at notify time. Everything else (INCREDIBLE-only, the programmatic dollar-savings thresholds, the watchlist 30-min DM gate) is unchanged.

## Why this is not a bandaid

- It does **not** weaken quality when the system is healthy — strict <10-min holds whenever the authenticated feed is available.
- It is **bounded** (60 min, configurable) — truly stale listings (90 min+) are still rejected; it does not open the floodgates to the multi-hour anon backlog.
- It only INCREDIBLE deals (the public min-score is untouched), so it is "older but still genuinely incredible," not "spam."
- It directly serves the product goal (the operator gets deals) and reduces the brittle dependency on a live FB session for ANY value — complementing, not replacing, the auth-feed work (the authenticated feed still gives the freshest just-listed deals when seeded).

This supersedes the implicit "10-min is absolute" reading of [[triage-freshness-converge-with-notify]] for the auth-down case only; the strict bar remains the auth-up rule.

## Validation

- `tests/unit/test_patrol_engine.py::TestAdaptivePublicFreshness`: auth-down relaxes (30-min INCREDIBLE notified), auth-up stays strict (30-min skipped), and even auth-down rejects truly-stale (90-min skipped).
- Prod: while auth is down, INCREDIBLE deals ≤60 min should now post to the public channel (with the heartbeat still nudging to re-seed cookies for the freshest feed).
