---
type: architecture
status: active
date: 2026-03-30
tags: [marketplace, scanner, facebook, scrolling]
related: [[facebook-graphql-anonymous-client]] [[listing-freshness-verification]] [[unified-filter-pipeline]]
---

# Facebook Marketplace — scrolling, listing volume, extraction

## Purpose

Extract fresh listings from Facebook Marketplace at a pace the ranker + rate limits tolerate, across two extraction paths (anonymous GraphQL + browser DOM).

## Shape

Two extraction paths:

- **Anonymous GraphQL** (`__user=0`) — direct HTTP POST, no browser. Structured data with `creation_time`, seller info, condition, price. Rate limited by IP (~6s between requests is safe). Pagination via `end_cursor` in `page_info`. See [[facebook-graphql-anonymous-client]]. `redacted_description` is NOT in search results — only in detail-page queries.
- **Browser DOM extraction** — scrolls the page, JS finds `a[href*="/marketplace/item/"]` links, walks to card container. Gets title, price, location, thumbnail. No timestamps unless freshness badge is parsed. Slower (15-30s per search with scrolling).

GraphQL is primary for everything. DOM is fallback only when GraphQL completely fails.

## Infinite scroll mechanics

- Initial page renders ~24 listings.
- FB's JS triggers a background GraphQL fetch past ~80% of content height.
- Each lazy-load appends 12-24 more listings.
- Feed exhausts when `scrollHeight` plateaus.
- `sortBy=creation_time_descend` is both a URL parameter AND a GraphQL variable — both must be set.
- Research (March 2026) confirms this sort is "severely bugged or deliberately deprecated" — Facebook frequently ignores it and returns algorithmic sort. Verify sort ourselves post-fetch via `_verify_sort_order`.

## Facebook ignores its own filters

- `daysSinceListed=1` is a **request**, not a guarantee. Facebook routinely injects older listings to pad results.
- Same applies to `commerce_search_and_rp_ctime_days` in GraphQL variables.
- Our freshness filter (`FreshnessFilter` in `listing_filter.py`) is the **hard enforcement layer** — drops listings with `posted_at` older than `listing_max_age_hours` regardless of what Facebook served.
- Sort order violations are common: `_verify_sort_order` logs warnings when Facebook shuffles promoted/recommended listings into the "newest first" feed.

## Depersonalized collection (March 29, 2026)

The authenticated browser's search history (from watchlist keyword searches) contaminates Facebook's recommendation algorithm. General browse on the same session serves personalized results biased toward recent searches.

Two-pronged solution:

1. **Anonymous GQL category searches** — broad keyword searches (`electronics`, `furniture`, `appliances`, `free stuff`, `sporting goods`, `toys`, `tools`) via anonymous GQL. Each returns 20-50 diverse listings. Config: `patrol_anonymous_browse_categories` in `AppConfig`.
2. **Separate anonymous browser** — a second headless `BrowserManager` with no login, no cookies, no search history. Uses DOM scraping with `sortBy=creation_time_descend` for the true "newest listings" empty-query feed.

GQL category searches give breadth (200-400 listings across 8 categories). The anonymous browser gives the true chronological feed the authenticated browser can't provide. Together they replace the broken single-query anonymous browse.

Fallback: if both anonymous sources fail, DOM from the auth browser is used — personalized results are better than none.

## Scroll-until-stable pattern

Fixed scroll-step counts lose listings. Instead: scroll down, check `document.documentElement.scrollHeight` after each step. When height stops increasing for 3 consecutive scrolls, feed is exhausted.

- `page.evaluate()` on browser-use sometimes returns a string instead of int for scrollHeight — always coerce with `int()`.
- Scroll parameters: 400-1000 px per step, 90% down / 10% small upward jitter, 800-2500 ms pause (longer pauses let FB lazy-load complete).
- First 3 scrolls always go down to get past sponsored listings quickly.

## Pagination

- Responses include `page_info.end_cursor` and `page_info.has_next_page` alongside `edges`.
- Pass `cursor` in GraphQL variables to fetch the next page.
- Anonymous browse endpoint (no query) paginated to 3 pages (50-75 listings).
- Keyword searches return 20-60+ results across 2-4 pages.
- `"edges":[]` with `text/html` content-type is a valid "no more results" response, NOT an error. Don't retry or re-bootstrap.
- Max 3 pages per search is the sweet spot — page 4 is almost always empty and wastes a rate-limit cycle.

## Listing descriptions — the gap

Facebook's GraphQL search API does NOT return `redacted_description` in search results — only in detail-page queries (`CometMarketplaceItemDetailQuery`). So all search-origin listings have empty descriptions.

The detail-page path was initially assumed to be OG/JSON-LD extraction. Confirmed dead; replaced with the data-sjs three-tier strategy. See [[detail-enrichment-empty-og-jsonld]].

Current extraction architecture (detail page, three-tier):

- **Tier 1 — `data-sjs` script tag parsing** (PRIMARY, WORKING): title, description, price, creation_time, condition, seller, photos, GPS from ScheduledServerJS payloads.
- **Tier 2 — DOM structural selectors**: price text, location, freshness badge.
- **Tier 3 — page markdown text parsing**: last resort for freshness + seller info.

## Rate limiting & concurrency

- GraphQL: 6 seconds between requests (down from 12; no 429s at 6s).
- GraphQL searches run concurrently (2 at a time via semaphore) because they're anonymous HTTP POSTs — no browser fingerprint linking them.
- Browser DOM searches must be serial — Facebook's bot detection tracks concurrent tab patterns.
- Inter-search stealth delay (8-12s) is only needed when the browser was used (DOM fallback). GraphQL has its own rate limiting.
- Detail page enrichment: 2 concurrent tabs is safe for a single logged-in account. 3+ risks detection.

## Timing benchmarks (9 watchlist items, ~13 search configs)

- Before optimization: ~10.5 min (serial GraphQL with 12s delay + 10s inter-search delays)
- After optimization: ~3.5 min (concurrent GraphQL with 6s delay, no inter-search delay for GQL-only)
- Detail enrichment of 50 listings: ~4 min at 2-tab concurrency

## Post-enrichment freshness re-check

Enrichment extracts `creation_time` from data-sjs, but the original freshness filter runs before enrichment. A second freshness check after enrichment drops listings whose `creation_time` exceeds `listing_max_age_hours`. Catches stale listings Facebook injects despite `daysSinceListed=1`.

## Invariants

- Freshness is enforced at multiple layers, with notification-facing cutoffs distinct from pipeline cutoffs — see [[just-listed-rework]].
- `edges:[]` is not an error state.
- Stealth delays are only for browser actions; GraphQL has its own rate limiting.
- `amount_with_offset*` fields are CENTS, not dollars — see [[graphql-amount-with-offset-cents-bug]].
