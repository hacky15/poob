---
type: architecture
status: active
date: 2026-04-01
tags: [pipeline, scanner, patrol, system-overview]
related: [[poobbrain-architecture]] [[facebook-scrolling-and-listing-volume]] [[vlm-triage-pipeline]] [[unified-filter-pipeline]] [[patrol-backoff-during-voice]] [[voice-architecture]]
---

# System pipeline — end-to-end

Complete pipeline documentation. Every stage, every data flow, every decision point. Complements the subsystem-level notes by showing how collect → filter → enrich → evaluate → match → notify fit together.

---

## System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        PATROL SCHEDULER                             │
│  Peak: 3min | Moderate: 10min | Off-peak: 15min | Dead: 30min     │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     PATROL CYCLE (PatrolEngine)                     │
│                                                                     │
│  Phase 1: COLLECT ──► Phase 2: FILTER ──► Phase 3: ENRICH          │
│  Phase 4: EVALUATE ──► Phase 5: MATCH ──► Phase 6: NOTIFY          │
└─────────────────────────────────────────────────────────────────────┘
```

**Voice-priority backoff:** the scanner and the real-time voice pipeline run in
the **same process** on a CPU-only box. The scheduler **skips a cycle while
users are actively in a voice channel** (voice-activity beacon recent) so voice
inference isn't starved, resuming once voice is quiet. Cycles are deferred, not
dropped. See [[patrol-backoff-during-voice]].

---

## Phase 1: COLLECT — Listing Discovery

### 1A: General Browse (Anonymous GraphQL + DOM)

```
Anonymous GraphQL POST (__user=0)
  ├─ Single page (browse endpoint returns few results)
  ├─ Returns: title, price, description, seller, creation_time, condition, images
  └─ If results > 0: primary source

Browser DOM Sweep (supplementary)
  ├─ Navigate to marketplace/?sortBy=creation_time_descend
  ├─ scroll_until_stable() — scrolls until scrollHeight plateaus
  ├─ JS extraction: a[href*="/marketplace/item/"] → card → text/price/image
  └─ Merged with GraphQL (GraphQL authoritative, DOM fills gaps)
```

### 1B: Watchlist Keyword Searches (Concurrent GraphQL + DOM fallback)

```
For each watchlist item × search config (location/price/condition):

  Phase 1: Concurrent GraphQL (2 at a time via semaphore)
    ├─ search_all_pages() — up to 3 pages, 50 items/page
    ├─ Cursor-based pagination via end_cursor
    ├─ 6s rate limit between pages
    └─ Tagged with _watch_item_id, _watch_interest

  Phase 2: Serial DOM fallback (only if GraphQL < 5 results)
    ├─ sweep_search() with scroll_until_stable
    ├─ Relevance filter drops obviously wrong results
    └─ 8-12s stealth delay between browser navigations

  Merge: GraphQL primary, DOM fills gaps (dedup by external_id)
```

### Listing Model — Fields Populated at Collection

| Field | GraphQL | DOM | Notes |
|---|---|---|---|
| `external_id` | From `id` | From URL regex | Always present |
| `title` | `marketplace_listing_title` | Card text parsing | May be "Just listed" from DOM |
| `price` | `listing_price.amount` (tries 3 field names) | `$` regex extraction | **Can be None from either source** |
| `description` | `redacted_description.text` | Empty (DOM can't extract) | Filled later by detail enrichment |
| `location` | `reverse_geocode.city_page.display_name` | Card text parsing | May be missing |
| `image_urls` | `primary_listing_photo` + `listing_photos` | First CDN image in card | GraphQL gets multiple images |
| `seller_name` | `marketplace_listing_seller.name` | Not extracted | Filled later by detail enrichment |
| `posted_at` | `creation_time` (unix → datetime) | `_parse_freshness()` on badge text | **Often None from DOM** |
| `is_sponsored` | `is_marketplace_boost_listing` or tracking text | innerText/aria-label/ads link | Filtered out in Phase 2 |
| `raw_data` | condition, posted_at_raw | Full card data | Accumulates metadata through pipeline |

---

## Phase 2: FILTER — Dedup, Freshness, Exclusions

### 2A: Batch Deduplication
```
filter_new_ids(site, external_ids)
  ├─ Single SQL query: SELECT external_id WHERE site=? AND external_id IN (?)
  ├─ Returns set of IDs NOT in database
  ├─ New listings saved immediately
  └─ Fallback: individual exists() checks if batch fails
```

### 2B: Freshness Filter (`_filter_stale`)
```
For each listing:
  ├─ ALWAYS DROP: is_sponsored=True
  ├─ ALWAYS DROP: location contains "ship" + "you"/"nationwide"
  │
  ├─ IF posted_at is not None AND posted_at < (now - 6 hours):
  │     └─ DROP (stale)
  ├─ IF posted_at is not None AND posted_at >= cutoff:
  │     └─ KEEP (fresh, verified)
  └─ IF posted_at is None:
        └─ KEEP (can't verify, but dedup prevents re-evaluation)

Age distribution logged: min/max/avg hours, fb_filter_violations count
```

### 2C: Category Exclusion
```
Drop titles/descriptions matching: vehicles, motorcycles, housing, couches
Watchlist-matched listings: EXEMPT (user explicitly searched for them)
```

### 2D: Sort Order Verification (diagnostic only)
```
Check timestamped listings are in descending posted_at order
Log warning on violations (Facebook injects promoted listings)
Does NOT filter — informational only
```

### 2E: Pre-Enrichment Priority Cap
```
Sort: watchlist-matched first, then general listings
Cap at deal_radar_max_evaluations (default 50)
Excess listings stay in DB as evaluated=0 (backlog)
```

---

## Phase 3: ENRICH — Detail Pages

### Current State: BROKEN (OG/JSON-LD extraction returns empty)
**As of March 2026:** OG tags are only served to crawler UAs, JSON-LD doesn't exist on Facebook. The detail extractor navigates to pages but extracts nothing useful. See `docs/technical_notes.md` and `docs/research/fb-detail-page-extraction.md`.

**Additionally:** Even when enrichment worked in-memory, the database ON CONFLICT clause was missing `description` and `posted_at`, so enriched values were silently dropped.

### Planned Fix: GraphQL Network Interception
```
For each listing (up to 50, 2 concurrent browser tabs):
  ├─ SKIP if description already > 10 chars (GraphQL source)
  │
  ├─ Navigate to listing URL
  ├─ CDP Network interception: capture /api/graphql/ responses
  │     Look for: CometMarketplaceItemDetailQuery response
  │     Extract: title, description, price, condition, creation_time,
  │              all photos, seller info, GPS coordinates
  │
  ├─ Tier 2 fallback: Parse data-sjs script tags for embedded Relay JSON
  │     Regex for marketplace_listing_title, formatted_amount, etc.
  │
  ├─ Tier 3 fallback: DOM structural selectors
  │     h1 span[dir="auto"] for title
  │     [role="main"] for content
  │     img[src*="scontent"] for images
  │
  ├─ Fill missing fields (never overwrite existing good data):
  │     price: only if listing.price is None
  │     posted_at: from creation_time or freshness badge text
  │     description: from redacted_description.text
  │     location: from location_text
  │     seller_name: from marketplace_listing_seller.name
  │
  └─ Save enriched listing to DB (description + posted_at in UPDATE SET)
```

### Fields After Enrichment (Once Fixed)

| Field | Before | After | Notes |
|---|---|---|---|
| `title` | May be garbage ("Just listed") | GraphQL `marketplace_listing_title` | Always updated if current is garbage |
| `description` | Empty (DOM) or partial (GraphQL) | `redacted_description.text` (full text) | Critical for triage and VLM |
| `price` | None or correct | Filled from GraphQL `listing_price.amount` | Never overwrites existing |
| `posted_at` | None (DOM) or correct (GraphQL) | From `creation_time` (Unix timestamp) | Never overwrites existing |
| `image_urls` | 1 image (DOM) or multiple (GraphQL) | All `listing_photos[].image.uri` | More images = better VLM eval |
| `raw_data` | Minimal | + condition, seller, GPS, all GraphQL fields | Full structured data available |

---

## Phase 4: EVALUATE — Three-Stage Pipeline (SmartDealRadar)

### Stage 1: Text Triage (Cloud LLM, batches of 5)

**Input to LLM:**
- Title, price (as "$X", "FREE", or "Price not listed"), description (300 chars), location, seller, condition
- Watchlist items with preferences/constraints
- Batch of 5 listings per prompt

**Processing:**
- LLM classifies each listing as investigate/filter
- Safety valve: minimum 15% pass-through (rescued by brand/price scoring)
- Auto-reject on severe scam signals (off-platform, Zelle, wire transfers)

**Bypasses (skip triage entirely):**
- Watchlist-tagged listings (`_watch_item_id` in raw_data) — triage kills 77% of valid matches
- Garbage titles ("Just listed", too short) — VLM with images is the only useful evaluator
- Backlog listings from previous cycles

**Output:** investigate=True/False, urgency_signals, scam_signals, misspelling_bonus

### Stage 2: Visual Enrichment + Comparable Sales

**2A: Visual Enrichment (parallel with 2B)**
```
Tier 1: Google Cloud Vision API (1,000/month)
  ├─ Web Detection → product name, brand, web entities
  ├─ OCR → text in image
  └─ Cross-validated against listing title (Jaccard overlap ≥ 0.4)

Tier 2: SerpAPI Google Lens (250/month)
  ├─ Visual matches + knowledge graph
  └─ Price extraction from results

Tier 3: No enrichment (VLM works from listing data alone)
```

**2B: Comparable Sales**
```
eBay Lookup (primary):
  ├─ Search query from validated enrichment or listing title
  ├─ Extract sold prices via regex ($X.XX pattern)
  ├─ IQR outlier removal
  └─ Returns: median, average, min, max, sample_count, confidence

Retail MSRP Lookup (fallback, only if eBay found 0 results):
  ├─ Search: "[brand] [model] [name] retail price MSRP"
  ├─ LLM extracts price from search results
  ├─ Sanity check: reject if MSRP > 15× listing price (wrong product)
  └─ Returns: median_price (MSRP), sample_count=1
```

### Stage 3: VLM Deep Evaluation (Vision models with voting)

**Full Context Provided to VLM:**

| Context | Source | Reliability |
|---|---|---|
| 2 listing images | First + last from image_urls | Depends on listing |
| Title + Listed Price | Listing model | Price may say "Not listed" |
| Description (500 chars) | OG or page text | Truncated |
| Location, Seller, Posted date | Listing model | May be Unknown |
| Seller-stated original price | Regex from description | May extract wrong price |
| Comparable sales (eBay/MSRP) | Stage 2B output | May be empty or wrong product |
| Enrichment data (product/brand/model) | Stage 2A output | May be generic/hallucinated |
| Condition signals | Regex patterns on description | Heuristic |
| Model numbers | Regex patterns on title/desc | May false-match |
| Title quality score (0-1) | Computed (caps, emojis, length) | Reliable |
| Freshness bonus | Computed from posted_at | Missing if no timestamp |
| Multi-item flag | Bulk/lot/collection detection | Heuristic |
| Triage signals | Stage 1 output | LLM-generated |
| Watchlist context | Interest, max budget, constraints | User-provided |

**VLM Output:** item_identified, condition, estimated_value (low/mid/high), deal_quality, confidence, reasoning, red_flags

**Voting:** 3 diverse VLM providers evaluate in parallel. Majority vote wins. Tiebreaker escalation if no consensus.

### Post-VLM Programmatic Enforcement

```
1. Preference Contradictions:
   Negations ("not metal"), positive constraints ("only cups"), bulk constraints
   → REJECT if any preference fails

2. Misleading Listing Detection:
   Trades, popup events, bait pricing, hidden pricing
   → REJECT

3. Unbranded Value Cap:
   No brand/model detected AND VLM confidence < 0.7
   → Cap estimated_value at $100 (or $500 for high-value categories)

4. Seller-Stated Price Cap:
   "Originally paid $X" in description
   → Cap estimated_value_mid at seller_price × 1.1

5. Value > Price Sanity Check:
   If VLM says market_value ≤ listing_price → FAIR

6. Dollar Savings Enforcement (ONLY when listing.price is not None):
   GOOD:       15%+ AND $10+ saved
   GREAT:      30%+ AND $30+ saved
   INCREDIBLE: 50%+ AND $75+ saved
   When price unknown: VLM score stands, capped at GREAT max
```

---

## Phase 5: MATCH — Interest Matching

```
Two parallel deal streams:

A. Base Deals (public channel):
   ├─ Score ≥ deal_public_min_score (default: INCREDIBLE)
   ├─ Not in excluded categories
   └─ Title not a UI artifact

B. Watchlist Deals (user DMs):
   ├─ Matched via _watch_item_id tag (origin tracking)
   ├─ OR matched via InterestMatcher keyword matching:
   │     All interest words must appear in title (plural/singular tolerant)
   │     Synonym expansion (e.g., "ps5" → "playstation 5")
   ├─ Score ≥ user's notification threshold
   └─ User's exclusion keywords checked
```

---

## Phase 6: NOTIFY — Discord Delivery

```
Base Deals → Public #facebook-marketplace channel
Watchlist Deals → User DMs with matched interest context

Deal embed includes:
  - Title, price, location, image
  - Deal score (color-coded)
  - Estimated market value, discount percentage
  - VLM reasoning summary
  - Direct link to listing
```

---

## Price Handling — The Golden Rules

```
listing.price = None   → "Price not listed" (triage), "check image" (VLM)
                        → Savings enforcement SKIPPED, VLM score stands (capped at GREAT)
                        → MSRP ratio check skipped
listing.price = 0      → "FREE" (triage/VLM)
                        → 100% discount, savings = full MSRP
listing.price > 0      → "$X.XX" (triage/VLM)
                        → Full savings enforcement applied

NEVER use: listing.price or 0.0  (converts None to 0, creates fake "free" items)
ALWAYS use: listing.price is not None  (distinguishes unknown from free)
```

---

## Data Quality Diagnostics (Per Cycle)

The system logs these metrics every patrol cycle:

| Metric | Log Key | What It Means |
|---|---|---|
| `timestamped=N` | Listing age distribution | How many listings have verified posted_at |
| `no_timestamp=N` | Freshness filter | How many slipped through without age verification |
| `fb_filter_violations=N` | Listing age distribution | Facebook served listings older than daysSinceListed |
| `stale=N` | Freshness filter | How many caught and dropped by age cutoff |
| `sort_violations=N` | Sort order verification | Facebook shuffled promoted listings into feed |
| `sponsored=N` | Freshness filter | Sponsored listings removed |
| `gql_searches=N` | Watchlist sweep | GraphQL searches attempted |
| `dom_fallback_searches=N` | Watchlist sweep | DOM fallbacks triggered |
| `auto_investigate=N` | Triage bypass | Watchlist items that skipped triage |

---

## External Service Dependencies

| Service | Purpose | Free Tier | Status |
|---|---|---|---|
| **Groq** | Agent brain, text triage, vision | 30 RPM, 14,400 RPD | Active |
| **Cerebras** | Text triage (primary when available) | 1K RPM, 64K TPM | Model-dependent |
| **NVIDIA NIM** | Browser agent, agent brain fallback | Free tier | Active |
| **Google AI Studio** | Gemini Flash/Pro VLM, Gemma 27B | 250-1000 RPD | Rate-limited in bursts |
| **OpenRouter** | Mistral/Nemotron VLM fallback | Free tier models | Rate-limited |
| **Google Cloud Vision** | Visual enrichment Tier 1 | 1,000/month | Quota-tracked |
| **SerpAPI** | Visual enrichment Tier 2 | 250/month | Often exhausted |
| **Tavily** | Web search (comparable sales) | 1,000/month | **Exhausted** |
| **Serper.dev** | Web search fallback | 2,500 one-time | **Exhausted** |
| **SearXNG** | Web search (self-hosted) | Unlimited | **Primary search provider** |

---

## Configuration Reference

### Patrol Timing
- `patrol_peak_interval_seconds`: 180 (3min, 4-9pm)
- `patrol_moderate_interval_seconds`: 600 (10min)
- `patrol_offpeak_interval_seconds`: 900 (15min)
- `patrol_dead_interval_seconds`: 1800 (30min, midnight-4am)

### GraphQL
- `patrol_graphql_min_delay_seconds`: 6.0 (between pages)
- `patrol_graphql_max_pages`: 3 (per search)
- `patrol_graphql_concurrency`: 2 (concurrent search streams)
- `scan_max_listings_per_query`: 50 (per GraphQL page)

### Scrolling (DOM fallback)
- `patrol_scroll_steps_category`: 20 (max scrolls for browse)
- `patrol_scroll_steps_search`: 30 (max scrolls for keyword)
- `patrol_scroll_until_stable`: True (smart scroll exhaustion)
- `patrol_scroll_max_stable_checks`: 3 (consecutive stable → stop)

### Deal Thresholds
- `deal_public_min_score`: "incredible" (public channel)
- `deal_watchlist_min_score`: "good" (user DMs)
- `deal_radar_max_evaluations`: 50 (per cycle)
- `listing_max_age_hours`: 6 (freshness cutoff)
