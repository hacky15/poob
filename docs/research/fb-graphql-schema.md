# Research: Facebook Marketplace GraphQL API — Schema & Anonymous Access
**Date:** March 17, 2026 | **Status:** CONFIRMED | **Sources:** results_2.md, results_4.md

## Anonymous Access (__user=0)

### Still Works (with caveats)
- `__user=0` search queries return: title, price, photos, location, seller name, pagination
- Prices ARE included for standard listings — our missing prices are a parsing bug
- Rate limiting is aggressive on datacenter IPs — residential proxies recommended for scale
- results_4.md says "effectively dead" but results_2.md confirms it works with proper tokens

### Required Parameters
```
POST https://www.facebook.com/api/graphql/
Body (form-encoded):
  doc_id: 7111939778879383
  variables: {JSON}
  __user: 0
  __a: 1
  av: 0
  lsd: {extracted from page HTML}
  fb_api_caller_class: RelayModern
  fb_api_req_friendly_name: CometMarketplaceSearchContentContainerQuery
```

### Dynamic Parameters (help but may be optional)
- `__dyn`, `__csr`, `__rev`, `__spin_r/b/t`, `__hsi` — session-specific, change per page
- Some scrapers successfully omit them

## Price Field Formats

| Field | Unit | Type | Example | Use For |
|-------|------|------|---------|---------|
| `listing_price.amount` | Dollars | String | "18.00" | Primary parsing |
| `listing_price.amount_with_offset_in_currency` | Cents | String | "1800" | Search results |
| `listing_price.amount_with_offset` | Cents | String | "1800" | Detail pages |
| `listing_price.formatted_amount` | Display | String | "$18.00" | Fallback |
| `listing_price.currency` | ISO | String | "USD" | Context |

**CRITICAL**: `amount` = DOLLARS. `amount_with_offset*` = CENTS. Both are STRINGS, never numbers.

### Why Some Listings Lack Price
NOT because of __user=0 restrictions. Actual reasons:
1. **Vehicle/rental listings**: Use specialized pricing (financing, per-month) — standard `listing_price` is null
2. **"Call for Price" dealers**: Intentionally masked
3. **Free items**: `formatted_amount` = "$0", `amount` = "0"
4. **GraphQL polymorphism**: Different `__typename` resolves different fields

Check `__typename` of the listing node to understand which schema variant is being returned.

## Known doc_ids

| doc_id | Query Name | Status |
|--------|-----------|--------|
| 7111939778879383 | CometMarketplaceSearchContentContainerQuery | Active (our current) |
| 5585904654783609 | city_street_search (location lookup) | Active |
| 2022753507811174 | MarketplaceSearch (legacy 2019) | Deprecated |
| 3456763434364354 | MarketplaceSearchResultsPageContainerNewQuery (2019) | Deprecated |

### Query Names Without Known doc_ids
- `CometMarketplaceSearchContentPaginationQuery` — subsequent pages (same fields, takes cursor)
- `CometMarketplaceCategoryContentContainerQuery` — category browsing
- `CometMarketplaceFeedQuery` / `MarketplaceFeedQuery` — homepage feed
- `CometMarketplaceItemDetailQuery` / `MarketplaceListingProfileQuery` — **DETAIL PAGE** (100+ fields)
- `CometMarketplaceSellerProfileContentQuery` — seller profiles

### How to Capture Fresh doc_ids
1. Open DevTools → Network → filter `api/graphql`
2. Browse/search Marketplace
3. In Payload tab: extract `doc_id` and `fb_api_req_friendly_name`
4. Copy as cURL for complete replayable command
5. HAR export for offline analysis

## Response Schema (Search Results)

```
data.marketplace_search.feed_units
  ├── page_info
  │   ├── end_cursor (pagination token)
  │   └── has_next_page (boolean)
  └── edges[]
      └── node
          └── listing
              ├── id
              ├── __typename
              ├── marketplace_listing_title
              ├── listing_price {amount, formatted_amount, currency, amount_with_offset_in_currency}
              ├── strikethrough_price {amount, formatted_amount}
              ├── primary_listing_photo.image.uri
              ├── listing_photos[].image.uri
              ├── location.reverse_geocode.city_page.display_name
              ├── marketplace_listing_seller {name, id, __typename}
              ├── marketplace_listing_category_id
              ├── delivery_types[] (["IN_PERSON", "SHIPPING"])
              ├── is_live, is_sold, is_pending, is_hidden
              ├── creation_time (Unix timestamp — NOT always present in search)
              └── condition
```

### Fields NOT in Search (require detail page query)
- Full description (`redacted_description.text`)
- All photos (search only returns primary)
- Seller ratings, join date
- GPS coordinates
- Vehicle-specific fields (make, model, VIN, mileage)
- Shipping profiles, purchase protection

## Open-Source References
- **kyleronayne/marketplace-api** (~55 stars) — Python/Flask anonymous GraphQL wrapper, best reference
- **jongan69/fb-marketplace-api** — Fork with proxy support, retry logic, User-Agent rotation
- **CajuM/fb-graphql-schema** (38 stars) — Complete schema from APK decompilation
- **ksucpea/marketplacehelper** (53 stars) — Chrome extension intercepting GraphQL responses
- **Wes Bos gist** — Original __user=0 technique, chronicles schema evolution
