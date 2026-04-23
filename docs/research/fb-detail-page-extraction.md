---
type: research
status: active
date: 2026-03-17
tags: [marketplace, facebook, extraction, data-sjs]
related: [[detail-enrichment-empty-og-jsonld]] [[facebook-og-jsonld-are-dead]] [[facebook-scrolling-and-listing-volume]]
---

# Research: Facebook Marketplace Detail Page Data Extraction

Sources: results_1.md, results_4.md.

## Key Findings

### OG Meta Tags — Dead for Browser UAs
- Facebook invented Open Graph but only serves `og:title`, `og:description`, `og:image` to **crawler user agents** (Facebookbot, Googlebot, Twitterbot)
- Standard browser UAs get an empty HTML shell — `document.querySelector('meta[property="og:title"]')` returns null
- Even when OG tags appear for crawlers, they contain only truncated data

### JSON-LD — Does Not Exist
- Facebook uses Open Graph protocol, NOT schema.org/JSON-LD
- No `<script type="application/ld+json">` tags exist on any marketplace page
- This is permanent — Facebook has no incentive to add schema.org markup

### Where Data Actually Lives: Relay + GraphQL
- Facebook's "Comet" architecture (React SPA, built 2019-2020) uses Relay Modern (their GraphQL client)
- When you navigate to `/marketplace/item/123456/`, a GraphQL query fires to `/api/graphql/`
- Response contains 100+ fields: title, price, full description, condition, all photos, seller info, GPS, timestamps
- The Relay Store (`__RELAY_DEVTOOLS_HOOK__`) exists ONLY with DevTools extension — not usable for automation
- React fiber internals (`__reactProps$`) are fragile and version-dependent — not recommended

### The Correct Extraction Method: Network Interception
**Consensus across all research sources**: Intercept `/api/graphql/` responses via CDP Network events.

```javascript
// Response contains nodes with these paths:
data.node.__typename === 'MarketplaceListing'
data.viewer.marketplace_product_details_page.target
data.marketplace_product_details_page

// Fields available:
marketplace_listing_title / base_marketplace_listing_title
listing_price.formatted_amount  // "$18.00"
listing_price.amount            // "18.00" (dollars, STRING)
strikethrough_price.formatted_amount  // original price if reduced
redacted_description.text       // full description
condition                       // NEW, USED_LIKE_NEW, USED_GOOD, USED_FAIR
creation_time                   // Unix timestamp
primary_listing_photo.image.uri
listing_photos[].image.uri      // all photos
marketplace_listing_seller.name
location_text                   // "Wellsburg, WV"
location.latitude / longitude   // GPS coords
```

### Backup: Embedded Relay JSON in data-sjs Script Tags
Facebook injects data via `<script type="application/json" data-sjs>` tags containing ScheduledServerJS payloads. Regex extraction works:
```javascript
const scripts = document.querySelectorAll('script[type="application/json"][data-sjs]');
// Search for content containing 'marketplace_listing_title'
// Regex: /"marketplace_listing_title":"(.*?)"/
// Regex: /"formatted_amount":"(.*?)"/
// Regex: /"creation_time":(\d+)/
```

### Backup 2: DOM Structural Selectors
Most stable DOM selectors (accessibility compliance keeps them):
- `h1 span[dir="auto"]` — title
- `div > span[dir="auto"]:has(span)` — price
- `a[href*="/user/"] span[dir="auto"]` — seller name
- `img[src*="scontent"]` — listing images
- `[role="main"]` — content container
- `div[data-testid="marketplace-listing-card"]` — listing container (when present)

### Page Load Timing
- Streaming SSR: initial shell → JS chunks → React hydration → GraphQL data → render
- Full sequence: 5-10 seconds in headless browser
- Wait strategy: `networkidle` + wait for `[role="main"] img` + 2-3s buffer

### Login Wall
- 90%+ of Marketplace gated behind authentication as of Q1 2026
- Login modal appears ~2 seconds after navigation for anonymous users
- Pressing Escape dismisses it temporarily but data is still redacted
- Our authenticated browser session bypasses this

## Implications for Our System
1. Delete all OG and JSON-LD extraction code — it will never work
2. Implement CDP network interception for detail pages (same pattern as search interception)
3. Add data-sjs regex parsing as Tier 2 fallback
4. Add DOM structural selectors as Tier 3 fallback
5. Increase detail page wait time to allow GraphQL response to arrive
