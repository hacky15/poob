---
type: incident
status: resolved
date: 2026-05-24
tags: [scanner, enrichment, facebook, freshness, notifications]
related: [[listing-freshness-verification]] [[just-listed-rework]] [[common-scanner-pitfalls]]
---

# Husqvarna enrichment cross-contamination silently killed all notifications

## Symptom

Zero deal notifications had reached Discord — public channel or DMs — despite the patrol scheduler running ~140 cycles/24h and `scan_logs.deals_found > 0` on multiple cycles (1 deal at 18:32 UTC, 9 deals at 02:47 UTC). The deals table contained zero rows. The user reported "ive had zero indication from my discord profile that its been sacnning" after extended monitoring.

Representative 24h DB-wide funnel:

```
1,310 listings scraped
  └─ 1,075 evaluated (mark_evaluated called)
      └─ 230 have posted_at  (17.6% timestamp coverage)
          └─   1 listing was < 10 min old at scrape time
              └─   0 made it through the public notification gate
```

`scan_logs.deals_found` counts the in-memory `base_deals + watchlist_deals` returned by `SmartDealRadar.evaluate_batch`. The `deals` table is only written inside `_notify()`, after the freshness + score gates pass. The gap between `deals_found > 0` and `deals table = 0 rows` is deals blocked at the gate, never persisted.

## Root cause

Three concurrent, compounding bugs in the enrichment + evaluation path:

**1. Cross-contamination from Facebook redirects (primary leak).** When the original listing is sold/deleted/private, Facebook silently serves the marketplace recommendation feed at the detail URL — every recent prod cycle had multiple discards all landing on the same recommendation page ("Husqvarna Riding Lawnmower"). The discard branch in `detail_extractor.py` correctly detected the mismatch via title-overlap check, but:

- It KEPT `detail.posted_at` and `detail.condition` from the wrong listing's page, contaminating the freshness signal.
- It silently returned the original listing unchanged, with `got_new_data=False` in `_enrich_one`, so the listing was NOT marked as enriched/saved. **But the listing remained in `to_evaluate`** — reaching VLM with no description, no timestamp. VLM evaluated blind, freshness gate auto-rejected the result, and the listing got `evaluated=1` permanently — wasted VLM credit, polluted DB row, never could fire a notification.

**2. `got_new_data` ignored `posted_at`.** In `_enrich_one`:

```python
got_new_data = (
    (enriched.description and not listing.description)
    or enriched.title != listing.title
    or enriched.price != listing.price
)
```

A successful enrichment that extracted ONLY `posted_at` (e.g. anon-GQL listings with description already populated but no creation_time) would not be written back to `listings[idx]` and not saved to DB. The timestamp was extracted, then thrown away.

**3. JS extractor freshness regex narrower than Python.** The DOM-sweep JS (`EXTRACT_LISTINGS_JS`) only matched `"Just listed"` and `"Listed N (minutes|hours|days|weeks) ago"`. The Python `_parse_freshness` (broadened in Phase E) accepts ~12 additional formats: `"a minute ago"`, `"about an hour ago"`, `"5m ago"`, `"3h ago"`, `"2d ago"`, `"3mo ago"`, `Posted`/`Updated` prefixes, `yesterday`, `last week`. Anything matching only the broader patterns was silently dropped before `_parse_freshness` ever saw it.

Underlying contributing fact (documented in [graphql_interceptor.py](../../src/poob/browser/graphql_interceptor.py) lines 26-30): FB stripped `creation_time` from anonymous `__user=0` browse responses entirely, so the anon GQL path produces zero timestamps. All timestamps must come from DOM badge parsing or detail-page extraction — both of which were leaky.

## Fix

Four-part change, all in one commit:

1. **`src/poob/sites/facebook/detail_extractor.py`** — added `EnrichmentRedirectedError`. The redirect-discard branch now raises it instead of returning a contaminated listing. The outer `except Exception` re-raises this specific error so it propagates to the caller.

2. **`src/poob/scanner/patrol_engine.py` `_enrich_one`** — catches `EnrichmentRedirectedError`: marks the listing permanently evaluated in the DB (so it stops returning as backlog), flags the in-memory listing via `raw_data["_enrichment_redirected"]=True`, increments a `redirected_count`. Treats as a redirect, not a failure (does not advance the `consecutive_misses` early-bail counter).

3. **`src/poob/scanner/patrol_engine.py` `_evaluate`** — filters out listings with `_enrichment_redirected` from `to_evaluate` before SmartDealRadar runs. Logs the exclusion count.

4. **`src/poob/scanner/patrol_engine.py` `_enrich_one`** — `got_new_data` now also fires on `(enriched.posted_at is not None and listing.posted_at is None)`. Posted_at-only enrichments are saved.

5. **`src/poob/sites/facebook/js_extractor.py`** — broadened the JS freshness regex to cover all formats `_parse_freshness` accepts: word-form quantifiers (`an?`, `a few`), `about` prefix, abbreviated units (`\d+[mhdwy] ago`), month/year units, `Posted`/`Updated` prefixes, `yesterday`/`last week`.

6. **`PatrolCycleResult`** — added `enriched`, `enrichment_redirected`, `enrichment_failed`, `timestamp_coverage_pct`, `vlm_evaluated` fields. New `funnel.cycle` log line at end of every cycle exposes attrition at every stage:

```
funnel.cycle  total_seen=N  new=N  enriched=N  redirected=N  enrich_failed=N
              ts_coverage_pct=N  vlm_evaluated=N  deals=N  notified=N
```

## Validation

- 35 unit tests in `test_detail_extractor.py` + `test_js_extractor.py` pass, including 3 new tests covering the redirect-raise behavior (cross-contamination, partial overlap kept, short-title bypass).
- 192 tests across scanner + sites/facebook still pass — no regressions.
- Post-deploy: watch `funnel.cycle` log line in prod. A healthy cycle should now show `enriched + redirected ≈ to_enrich` (no silent fall-through) and `vlm_evaluated == enriched - eval_filter_drops` (no blind VLM calls on redirected listings).

## Follow-ups

- Cadence: 1 listing/24h was <10 min old at scrape time (UTC stats). Even with perfect enrichment, the eligible pool is structurally tiny unless the moderate-hours interval is tightened from 600s. Deferred until funnel telemetry shows whether the fix alone raises notification volume.
- 0 active watch items in the DB. DMs cannot fire by definition. User confirmed INCREDIBLE-only-to-public is still the standard regardless.
- The `Husqvarna Riding Lawnmower` cross-contamination pattern suggests Facebook serves a deterministic recommendation when listings are missing. Could be a cheaper pre-check (detect redirect by URL change rather than navigating + extracting + comparing titles), but only worth doing if the redirect rate stays high.
