---
type: architecture
status: active
date: 2026-04-05
tags: [scanner, filter, geo, freshness]
related: [[listing-freshness-verification]] [[garbage-listing-filter]] [[just-listed-rework]]
---

# Unified filter pipeline — `FilterChain` + predicate composition

## Purpose

One place for all listing-filter logic. Previously the patrol pipeline had fragmented filtering: 65+ hardcoded vehicle/housing keywords, a hardcoded US-state whitelist, and the same filter logic copy-pasted at pre-enrichment, post-enrichment, and notification stages. Backlog-recovered listings entered at `_evaluate()`, skipping ALL filters — vehicles, stale listings, and out-of-region listings reached notifications.

## Shape

`src/poob/scanner/listing_filter.py` + `FilterChain`. Predicate composition: each filter is a frozen dataclass with `__call__` → `FilterVerdict`. `FilterChain` orchestrates execution, stage routing, and tag-based exemptions.

## Filters

| Filter | Stage | What It Does |
|--------|-------|--------------|
| `SponsoredFilter` | PRE_ENRICHMENT | Rejects sponsored/boosted listings and "Ships to you" |
| `CategoryFilter` | PRE + POST_ENRICHMENT | Uses `marketplace_listing_category_id` from GraphQL (100% reliable), falls back to keyword matching |
| `FreshnessFilter` | PRE + POST_ENRICHMENT | Rejects listings older than `listing_max_age_hours` |
| `GeoDistanceFilter` | POST_ENRICHMENT | Haversine distance from user's configured center point, using lat/lng from detail-page enrichment |
| `GarbageFilter` | POST_ENRICHMENT | Rejects UI artifacts ("See details", "Loading") and unenrichable titles with no description |

## Tag-based exemptions

Watchlist-matched listings get a `watchlist_category_override` tag that exempts them from `CategoryFilter` (but NOT geo / freshness / garbage). Tags are computed upstream; filters don't know about watchlists.

## Backlog fix

Backlog listings now go through the full filter chain (both stages) before evaluation. Rejected backlog listings are marked `evaluated=True` so they don't reappear.

## Geo-filtering — haversine distance

`src/poob/utils/geo.py` — pure `math` module, no external dependencies.

- Center point resolved from `patrol_center_lat` / `patrol_center_lon` config, auto-resolved from `marketplace_default_location` city slug if unset.
- Listings without coordinates pass through (benefit of the doubt — coordinates only available after detail enrichment).
- Null island (0, 0) detected and treated as "no coordinates."
- Accurate to ~0.3% at Wisconsin latitudes — more than enough for marketplace filtering.

## GraphQL field extraction

`marketplace_listing_category_id` is extracted from search responses (was always in the response but never parsed). Also extracts `delivery_types` and attempts to extract `location.latitude` / `location.longitude` from search results.

Temporary debug log `DEBUG_GQL_KEYS` dumps all listing node keys for the first 3 listings per session. Remove after verifying field availability on next live run.

## Dead code flagged

- `src/poob/browser/detail_interceptor.py` — marked DEPRECATED. The CDP interceptor was built on the assumption that Facebook fires GraphQL on detail-page navigation; actually, detail data lives in `data-sjs` HTML tags. Active path is `detail_extractor.py` → `parse_data_sjs_payloads()`. See [[detail-enrichment-empty-og-jsonld]].

## Related utilities

- `_parse_evaluate_result` moved from `detail_extractor.py` to `src/poob/utils/content.py` as `parse_evaluate_result()`. Handles browser-use's `Page.evaluate()` returning JSON-stringified strings instead of Python objects. Should be used by any caller of `page.evaluate()`.

## Invariants

- **No bandaid category keyword growth.** If a category isn't caught, add it to the category-id mapping or improve keyword heuristics, don't duplicate the pattern elsewhere.
- **No US-state whitelist.** Geo is distance-based; `ALLOWED_NOTIFY_STATES` is gone.
- **Backlog never bypasses filters.** Any path that inserts into the evaluation queue must go through the chain.
- **Freshness is checked multiple times** — pre-enrichment is the pipeline cutoff, post-enrichment catches FB's stale padding, and [[just-listed-rework]] adds a separate notification cutoff.

## Related

- [[listing-freshness-verification]] — age sources, distributions, fb-filter violations.
- [[garbage-listing-filter]] — what counts as garbage and when to filter.
- [[just-listed-rework]] — notification-level freshness, separate from pipeline freshness.
