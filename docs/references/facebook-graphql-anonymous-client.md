---
type: reference
status: active
date: 2026-03-20
tags: [marketplace, facebook, graphql]
source: "Facebook Marketplace, unofficial"
related: [[facebook-scrolling-and-listing-volume]] [[graphql-amount-with-offset-cents-bug]]
---

# Facebook GraphQL anonymous client

## Summary

A direct HTTP client posting to `facebook.com/api/graphql/` as `__user=0` (anonymous viewer). Eliminates user-history personalization — results are based purely on location, query, and recency. Primary extraction path for search; detail-page data now comes from `data-sjs` HTML tags rather than this client.

## Session bootstrapping

1. GET `facebook.com/marketplace/` to receive `datr` cookie + extract `lsd` token from HTML.
2. POST `/api/graphql/` with `__user=0`, `datr` cookie, `lsd` token.

## Response format quirks

- Facebook often returns GraphQL JSON with `text/html` content-type — check the body, not the content-type header.
- Responses prefixed with `for (;;);` (anti-XSSI) — strip before parsing.
- Empty `edges:[]` with a valid `page_info` cursor means "no more results for this query" — not an error, not a session problem.
- When the response has `errors` in the JSON, the `doc_id` is likely stale and needs refreshing.

## doc_id management

`doc_id` values are pre-registered query IDs that Facebook maps to server-side queries. They change infrequently but can break on redeploy.

- Current working ID: `7111939778879383` (MARKETPLACE_SEARCH_DOC_ID).
- When broken: valid HTTP 200 but JSON contains `errors` array.
- Refresh: open browser DevTools → Network → filter for `/api/graphql/` → search marketplace → copy `doc_id` from the POST body.

## Price field shapes

Facebook uses multiple names across endpoints. `amount` is DOLLARS, `amount_with_offset*` are CENTS, all are strings:

| Field | Units |
|---|---|
| `listing_price.amount` | Dollars |
| `listing_price.amount_with_offset` | Cents (detail pages) |
| `listing_price.amount_with_offset_in_currency` | Cents (search results) |
| `listing_price.formatted_amount` | Display string ("$18.00") |
| `listing_price.currency` | ISO code ("USD") |
| `strikethrough_price.formatted_amount` | Original price if reduced |

See [[graphql-amount-with-offset-cents-bug]] for the bug when cents were treated as dollars.

## Usage in this project

- [sites/facebook/graphql_client.py](../../src/poob/sites/facebook/graphql_client.py) — the HTTP client itself.
- [scanner/patrol_engine.py](../../src/poob/scanner/patrol_engine.py) — primary consumer.

## Source

Unofficial — Facebook's GraphQL surface is not publicly documented. Behavior is observed, not contracted. Anything here can change without notice.
