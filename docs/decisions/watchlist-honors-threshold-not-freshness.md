---
type: decision
status: active
date: 2026-06-04
tags: [scanner, watchlist, notifications, freshness, deal-radar]
supersedes: []
related: [[triage-freshness-converge-with-notify]] [[just-listed-rework]]
---

# Watchlist DMs honor the per-item notification_threshold, not deal-quality or freshness

## Context

A user's watch items (`under toilet cabinet`, `kitchen countertop stools`) had
**never** produced a single DM in ~10 days — 0 of 80+ deals carried a
`watch_item_id`. A scanner-only audit (2026-06-04) proved the watchlist was
**finding** matches every cycle (1–4 new listings; e.g. `listing.rejected
title='Vintage 1980s Handmade Pine Tissue/Toilet Paper Cabinet'`) but discarding
**100%** of them. Two gates, both wrong for a wishlist:

1. **Deal-radar global floor** (`orchestrator._vlm_to_deal`): every listing,
   watch or not, was gated by the global `deal_radar_min_score` (GOOD). A
   normally-priced cabinet scores FAIR (`vlm_score=pass`), so it was dropped as
   "below_min_threshold" — *before* the per-item `notification_threshold` was
   ever consulted. The user had set `notification_threshold='all'`, which only
   lives downstream at `_notify` (`elif user_threshold == "all": pass`), so it
   never received a deal to act on.
2. **Notify freshness gate**: even a surviving watch deal had to clear
   `posted_at is not None` AND age ≤ `watchlist_notification_max_age_minutes`
   (30). Niche items are almost never listed in the last 30 minutes when first
   scraped, so this would have dropped them too. And upstream, the
   `FreshnessFilter` at POST_ENRICHMENT rejects watch listings older than the
   6h triage window before they even reach the radar.

The user's intent, stated directly: *"it should be notifying me based on my
notification preferences. simple as that. i set it to 'all', so it should be
notifying me on all."*

A wishlist is categorically different from the public deal feed. The public feed
hunts **bargains that are about to be gone** — deal-quality and "just listed"
freshness are both load-bearing there. A wishlist answers **"is the specific
thing I want available?"** — discount and listing age are irrelevant; the user
wants to know it exists so they can buy it.

## Decision

For **watchlist-matched** listings only, the per-item `notification_threshold`
is the **sole** gate. Three coordinated changes:

1. **Radar gate is threshold-aware** (`orchestrator.py`). A new
   `_WATCH_THRESHOLD_TO_SCORE` maps the watch item's threshold to the deal-quality
   floor for that match: `all`/`free` → no floor (UNKNOWN), `good`/`great`/
   `incredible` → their `DealScore`. Non-watch listings keep the global
   `deal_radar_min_score`. (`free` is price-gated later at `_notify`.)
2. **Watchlist exempt from the freshness filter** (`listing_filter.py` +
   `patrol_engine.py`). `FreshnessFilter.exempt_tags` now includes
   `watchlist_freshness_override`, and watch listings carry that tag — so a
   watched item is never dropped for being older than the triage window, at PRE
   or POST enrichment.
3. **No freshness gate at notify for watchlist DMs** (`patrol_engine.py`). The
   minute-level `posted_at`/`skip_too_old` block is removed from the watchlist
   loop; batch dedup already guarantees one DM per listing. The **public**
   channel keeps its strict minute-level gate unchanged.

`watchlist_notification_max_age_minutes` is retired (dead config; `extra=ignore`
makes removal safe).

## Why supersede the watchlist part of `triage-freshness-converge-with-notify`

That decision (2026-05-25) converged triage with the notification gates and kept
a 30-min watchlist cutoff. Its reasoning was sound **for the public feed** — and
that part stands. But it implicitly applied "just-listed" semantics to the
watchlist, which defeats the wishlist's entire purpose for the niche items users
actually watch. This note narrows that: freshness still gates the public feed;
the watchlist is availability-based and gated only by the user's threshold.

## Consequences

- Watch items set to `'all'` now DM on **any VLM-confirmed match** (the
  `InterestMatcher` relevance check still prevents keyword garbage), regardless
  of price or listing age. Volume is bounded by distinct matching listings
  (rare for niche items) × dedup (one DM each).
- Watch items set to a quality threshold (`good`+) behave as before on
  deal-quality, but are no longer freshness-gated.
- Slightly more VLM spend: old watch matches now reach evaluation (previously
  freshness-filtered out). Watch volume is low (1–4/cycle), so the cost is small.
- Watch failure mode flips from "silently never fires" to "fires on availability"
  — the user can tighten with `notes`/exclusions or a higher threshold.

## Validation

9 new unit tests (radar threshold gate, freshness-filter exemption via the
chain, notify with no freshness gate); full suite green. Post-deploy: watch for
`deal_found ... is_watchlist=True` and a `send_deal_dm` for the watch items.
