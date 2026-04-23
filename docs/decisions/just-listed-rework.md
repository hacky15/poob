---
type: decision
status: active
date: 2026-04-22
tags: [scanner, patrol, notifications, freshness]
related: [[unified-filter-pipeline]] [[listing-freshness-verification]]
---

# "Just Listed" rework — split freshness knobs + auto-start scheduler

## Context

Two independent failures in the notification pipeline:

1. **Public channel spam**: `#facebook-marketplace` posted items that were neither clearly-a-deal nor clearly-just-listed. Config had `deal_public_min_score=incredible` but borderline scores, unknown-price items, and hours-old items still reached the channel.
2. **Watchlist DMs firing too broadly**: DMs triggered on items that weren't truly-recent AND weren't tight matches — the "cookies → baker mistakes a cookie jar hunt for baked goods" class of false positive.

Three root causes:

- **Scheduler not running.** `main.py` constructed `PatrolScheduler` but never called `scheduler.start()`. The container had 20h of uptime and zero patrol cycles — every user-visible notification was stale historical data. The old comment *"Scheduler is NOT auto-started — user triggers via !scan"* is incompatible with the current "monitor in near-real-time" goal.
- **Single freshness knob conflated two concerns.** `listing_max_age_hours = 6` was used for BOTH the evaluation pipeline cutoff AND the user-facing "just listed" notification gate. At 6h, any listing under a quarter-day old could reach the public channel — "today" not "just listed."
- **No coverage measurement.** Facebook runs the Marketplace feed through an AI ranker ([transparency docs](https://transparency.meta.com/features/explaining-ranking/fb-marketplace/)). Without ground truth, we can't tell if we're catching 20% or 95% of fresh listings.

## Decision

**Split the freshness knob.** Three fields in `AppConfig`:

| Field | Default | Purpose |
|---|---|---|
| `listing_max_age_hours` | 6 | Evaluation cutoff. Older listings drop from VLM pipeline. |
| `public_notification_max_age_minutes` | 10 | Public channel cutoff. "Just listed AND clearly worth something." |
| `watchlist_notification_max_age_minutes` | 30 | Watchlist DM cutoff. Slightly laxer — missing a specific-interest match hurts the user more than a stale public post. |

Enforced in `PatrolEngine._notify()`, NOT in the `FilterChain`. The filter-chain freshness still serves backlog recovery + observability. The notification gate is the product-facing enforcement layer; keeping them separate means we can measure stale-deliveries even when the filter pipeline starts dropping old items aggressively.

**Auto-start the patrol scheduler by default.** New field `patrol_scheduler_auto_start: bool = True`. `main.py` calls `await scheduler.start()` before the bot task group if enabled. Opt-out for debugging; default on because the product requires it.

## Why 10m / 30m

Flipping community benchmark: underpriced listings get their first message within ~8 minutes. The public-channel gate targets the window where a deal still *exists* to be caught. Watchlist is the user's specific interest — 30m covers a user who stepped away from Discord briefly without missing a truly-recent match.

## Observability (same deploy)

Added `src/poob/scanner/observability.py`. `measure_sweep()` computes:
- Age histogram: `0-5m / 5-15m / 15-60m / 60m-6h / >6h`
- `no_timestamp` count
- `fb_filter_violations` count (FB served items older than `daysSinceListed * 24h`)
- min/max age per sweep

Hooks in `PatrolEngine._sweep_and_intercept()` and the watchlist-sweep branch of `run_patrol_cycle`. Measurement is on the **raw** sweep output, before dedup and filtering — we want to see what Facebook is feeding us, not what survived our pipeline.

Healthy signature: mass in `0-5m`. Concerning: heavy `60m-6h` and/or high `fb_filter_violations`.

## Canary ground truth

Added `src/poob/scanner/canary.py` — in-memory registry of canary tokens.

1. Operator posts an FB Marketplace listing from a burner account with title containing `POOB-CANARY-{8-hex}`.
2. Operator registers the token via `engine.canaries.register(token, expected_at=...)`.
3. `check_batch()` scans every raw sweep. First observation emits `canary.detected` with `latency_seconds` and `source`. No detection within 20 minutes of `expected_at` → `canary.missed`.

Registry is intentionally in-memory. Canaries have 24h TTL; we don't want SQLite schema debt for a measurement fixture. The Discord command to register/deregister belongs in the `discord_bot` scope and hasn't landed yet — until then, register via `bot.patrol_engine.canaries.register(...)` in a debug shell.

## Deferred to next phase

Measurement first. Without histogram and canary SLA, each of the following is optimizing blind:

- **Identity pool** — multiple rotating anonymous Chromium contexts. Biggest single coverage lever per research; personalization can't act on an identity it has never seen.
- **Geographic sharding** — 3 offset centers within user radius.
- **Tag-originated watchlist identification re-check** — runs `InterestMatcher.match_with_identification` on tagged results before routing to DM. Fixes the "cookies → baker" class of false positive.
- **Public/DM routing correction** — if a post-VLM result matches ANY active watchlist, route DM-only, never public.

## Consequences

- Public-channel cutoff is now aggressive. Items 11+ minutes old won't post — this is by design. If it turns out 10m is too tight (empty public channel), loosen to 15m.
- Watchlist 30m is a first guess. Adjust based on observability data.
- Scheduler auto-start changes the deploy ceremony: a fresh container immediately starts sweeping. If this is ever undesirable (canary experiments, debug runs), set `patrol_scheduler_auto_start=false`.
