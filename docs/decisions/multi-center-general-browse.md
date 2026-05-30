---
type: decision
status: active
date: 2026-05-29
tags: [scanner, graphql, geo, location, coverage, rate-limit, madison, appleton]
related: [[browse-path-ignored-configured-location]] [[anon-browser-cdp-death-no-recovery]]
---

# Multi-center general browse (Madison + Appleton), $0 slow-cadence

## Context

The 2026-05-29 coverage audit answered the operator's "are we gathering everything we can for Madison." Findings that drove this decision:

- The general-browse GQL path was serving the wrong metro due to a hardcoded Appleton default (fixed in [[browse-path-ignored-configured-location]]).
- The operator clarified the intended **general** search area is **both Madison and Appleton/Fox-Valley** (distinct from user *watchlist* items, which carry their own per-item location bounds and are unaffected).
- The dominant volume constraint is FB hard-rate-limiting the anonymous GQL on the outbound IP (177/182 cycles returned zero; `consecutive_hits >130`). The operator chose to address this **$0 via slower cadence**, not a paid proxy.

The tension: two metros want more coverage, but the rate limit demands fewer GQL POSTs. Sweeping both metros' 8 categories every cycle would double POSTs (16 → 32/cycle) and trip the limit harder.

## Decision

**Rotate the general-browse center per cycle instead of sweeping all centers every cycle.**

1. New config `patrol_browse_locations: list[str] = ["madison", "appleton"]` — the general-search centers. (Watchlist searches are untouched; they already thread their own location.)
2. `PatrolEngine._fetch_anonymous_graphql` picks ONE center per cycle by rotating `_browse_location_idx` across `patrol_browse_locations` (Madison this cycle, Appleton next, …). All 8 categories query that one center. POST volume per cycle stays flat at one-center × N-categories (same as today), while both metros are covered across consecutive cycles. This is the $0 way to cover two metros without doubling request pressure.
3. `GeoDistanceFilter` gains `extra_centers: tuple[tuple[float,float], ...]`. A listing passes if within `radius_miles` of the primary center OR any extra center. Without this, the Madison-centered filter would reject every Appleton listing (~100mi away) — the exact downstream waste the geo fix was meant to eliminate. `PatrolEngine.__init__` resolves every `patrol_browse_locations` slug to coords and passes the non-primary ones as `extra_centers`.
4. `patrol_graphql_min_delay_seconds: 6.0 → 12.0` (restored). The 6s value (lowered from 12 for speed) was tripping the rate limit hard. Halving the request *rate* is the core $0 lever to stay under FB's limit. This is a throughput knob, not a correctness/safety-valve, but it was a deliberate prior setting so it's recorded here.

## Consequences

- Each metro is scanned every *other* moderate cycle (~every 10 min at the 5-min moderate interval). Freshness per metro halves vs single-metro-every-cycle — an accepted tradeoff for covering two metros under the rate limit at $0.
- The `LocationTextFilter` (state-centroid pre-check) needs no change: both Madison and Appleton are WI, so it already passes both and only rejects far states (CA/TX).
- The `KnownFarLocationFilter` learned cache still works per-location-string; multi-center just means fewer strings get learned-as-far. **(CORRECTED 2026-05-30 — see [[known-far-location-cache-poisoning]]: keying the cache on the raw label is unsafe because a single FB town label straddles the radius boundary. The filter now also tracks an in-radius "near" veto so a boundary town like "Madison, WI" is never cached wholesale.)**
- If the rate limit still dominates after this, the remaining levers are: fewer categories per cycle (rotate categories like the DOM tier), or a residential/Madison+Appleton egress proxy ($ — deferred, operator declined for now).
- Adding a third metro later = one entry in `patrol_browse_locations`; the rotation and multi-center filter scale without code change (registry pattern).

## Validation

- `TestGeoDistanceFilterMultiCenter`: passes near primary, passes near extra center (Neenah ~10mi from Appleton / ~95mi from Madison), rejects outside all, and a sanity test proving single-center still rejects the extra metro.
- `TestBrowsePathLocationThreading::test_browse_rotates_centers_across_cycles`: asserts center sequence [madison, appleton, madison] across 3 cycles.
- Full `test_patrol_engine.py` + `test_listing_filter.py` + `test_config.py` green.
- Post-deploy: `Anonymous GQL browse center center=madison|appleton` logs alternate per cycle; the DB no-coords region split should show BOTH Madison and Fox-Valley clusters (not Appleton-only).

## Follow-ups (unchanged from the audit, not addressed here)

- FB rate-limit is mitigated (slower rate) not solved; a proxy remains the real volume fix if $0 cadence proves insufficient.
- 67% NULL `posted_at` (extraction-layer fix) — next scanner task.
- Freshness-window vs cadence arithmetic — separate decision note required before changing `public_notification_max_age_minutes`.
