---
type: architecture
status: active
date: 2026-03-20
tags: [scanner, freshness, timestamps]
related: [[unified-filter-pipeline]] [[just-listed-rework]] [[facebook-scrolling-and-listing-volume]]
---

# Listing freshness verification

## Purpose

Keep stale listings out of evaluation and out of user-facing notifications. Facebook's own `daysSinceListed` parameter is advisory — they pad with older listings — so we enforce our own cutoffs.

## Timestamp sources

- **GraphQL `creation_time`**: Unix timestamp, reliable, parsed into `posted_at` datetime.
- **DOM freshness badge**: "Just listed", "Listed 3 hours ago", etc. Parsed by `_parse_freshness()` into approximate datetime.
- **Detail-page data-sjs**: `creation_time` extracted during enrichment.

## No-timestamp gap

DOM-scraped listings without a freshness badge get `posted_at=None`. These are kept (can't verify age, but dedup prevents re-evaluation). With GraphQL as primary, most listings now have timestamps. The `no_timestamp` count in freshness-filter logs shows how many slipped through.

## Age distribution logging

Each patrol cycle logs:

```
Listing age distribution  timestamped=45  min_hours=0.2  max_hours=4.8  avg_hours=2.1
                          within_cutoff=43  stale=2  fb_filter_violations=1
```

- `fb_filter_violations` = listings older than `daysSinceListed * 24h` that Facebook served despite the filter. This is proof Facebook ignores its own age parameters.

## Separate notification-level cutoffs

Pipeline freshness (`listing_max_age_hours`, default 6) is distinct from user-facing cutoffs (`public_notification_max_age_minutes`, `watchlist_notification_max_age_minutes`). See [[just-listed-rework]] for the split and its rationale.

## Observability

Sweep-level metrics (age histogram, `no_timestamp` count, `fb_filter_violations`) measured via `src/poob/scanner/observability.py` on the **raw** sweep output — before dedup and filtering. Tells us what Facebook is feeding us, not what survived our pipeline.

## Invariants

- Filter-chain freshness is the pipeline cutoff; `_notify()` holds the user-facing cutoff. Don't conflate them.
- `no_timestamp` listings pass through pipeline filtering but should be flagged in provenance so downstream consumers know.
- FB-filter-violation count is a product-health signal: if it's climbing, the coverage problem is FB serving stale padding, not our extraction.
