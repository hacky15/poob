# Technical Notes

Research findings, architectural decisions, and lessons learned.
Read this before making changes to avoid repeating past mistakes.
**OVERWRITABLE WHEN RESEARCH OR IMPLEMENTATION DEEMS CURRENT IS OUTDATED/WRONG, MUTABLE WHEN KEY FINDINGS ARE OBSERVED.**

---

## Facebook Marketplace Scrolling & Listing Volume

### How Facebook Infinite Scroll Works
- Initial page load renders ~24 listings
- Facebook's JS triggers a background GraphQL fetch when the user scrolls past ~80% of content height
- Each lazy-load batch appends 12-24 more listings to the DOM
- The feed eventually exhausts and `scrollHeight` plateaus
- `sortBy=creation_time_descend` is a URL parameter AND a GraphQL variable — both must be set
- **WARNING**: Research (March 2026) confirms this sort is "severely bugged or deliberately deprecated" — Facebook frequently ignores it and returns algorithmic sort. Must verify sort ourselves post-fetch.

### Facebook Ignores Its Own Filters
- `daysSinceListed=1` is a **request**, not a guarantee. Facebook routinely injects older listings to pad results.
- The same applies to `commerce_search_and_rp_ctime_days` in GraphQL variables.
- Our freshness filter (now `FreshnessFilter` in `listing_filter.py`) is the **hard enforcement layer** — it drops listings with `posted_at` older than `listing_max_age_hours` regardless of what Facebook served.
- Sort order violations are common: `_verify_sort_order` logs warnings when Facebook shuffles promoted/recommended listings into the "newest first" feed.

### Anonymous GraphQL vs Browser DOM: Two Extraction Paths
- **Anonymous GraphQL** (`__user=0`): Direct HTTP POST, no browser needed. Returns structured data with `creation_time`, seller info, condition, price. Rate limited by IP (~6s between requests is safe). Pagination via `end_cursor` in `page_info`. **NOTE: `redacted_description` is NOT included in search results** — only in detail page queries (CometMarketplaceItemDetailQuery). All search-origin listings have empty descriptions.
- **Browser DOM extraction**: Scrolls the page, runs JS to find `a[href*="/marketplace/item/"]` links, walks up to card container. Gets title, price, location, thumbnail. No timestamps (unless freshness badge is parsed). Slower (15-30s per search with scrolling).
- GraphQL is primary for everything. DOM is fallback only when GraphQL completely fails.

### General Browse Depersonalization (March 29, 2026)

**Problem**: The authenticated browser's search history (from watchlist keyword searches) contaminates Facebook's recommendation algorithm. When the general browse runs on the same browser session, Facebook serves personalized results biased toward recent searches (coffee makers, espresso, Legos, etc.) instead of truly "newest first" listings.

**Discovery (March 30)**: The anonymous GQL browse (empty query, `__user=0`) returns **~1 listing per cycle**. Facebook's anonymous browse endpoint doesn't serve a meaningful feed without search context. Every deal reaching the public channel was from watchlist-related searches leaking through as "public" deals.

**Solution (March 30): Two-pronged depersonalized collection:**

1. **Anonymous GQL category searches**: Instead of one empty-query browse, run broad keyword searches (`electronics`, `furniture`, `appliances`, `free stuff`, `sporting goods`, `toys`, `tools`) via anonymous GQL. Each returns 20-50 diverse, depersonalized listings. Config: `patrol_anonymous_browse_categories` in AppConfig.

2. **Separate anonymous browser**: A second headless `BrowserManager` with NO login, NO cookies, NO search history. Uses DOM scraping with `sortBy=creation_time_descend` for the true "newest listings" empty-query feed. Facebook can't personalize because there's no user identity at all.

**Why both**: GQL category searches give breadth (200-400 listings across 8 categories). The anonymous browser gives the true chronological feed that the authenticated browser can't provide. Together they replace the broken single-query anonymous browse.

**Post-enrichment freshness re-check**: Enrichment now extracts `creation_time` from data-sjs, but the original freshness filter runs before enrichment. A second freshness check after enrichment drops listings whose `creation_time` exceeds `listing_max_age_hours`. This catches stale listings that Facebook injects despite `daysSinceListed=1`.

**Fallback**: If both anonymous sources fail, DOM from the auth browser IS used — personalized results are better than no results.

### GraphQL Pagination
- Facebook's GraphQL responses include `page_info.end_cursor` and `page_info.has_next_page` alongside the `edges` array.
- Pass `cursor` in the GraphQL variables to fetch the next page.
- The anonymous browse endpoint (no query) is paginated to 3 pages (50-75 listings).
- Keyword searches return 20-60+ results across 2-4 pages.
- Page responses with `"edges":[]` and `text/html` content-type are valid "no more results" responses, NOT errors. Don't retry or re-bootstrap the session.
- Max 3 pages per search is the sweet spot — page 4 is almost always empty and wastes a rate-limit cycle.

### Scroll-Until-Stable Pattern
- Instead of fixed scroll step counts, scroll down and check `document.documentElement.scrollHeight` after each step.
- When height stops increasing for 3 consecutive scrolls, the feed is exhausted.
- `page.evaluate()` on browser-use sometimes returns a string instead of int for scrollHeight — always coerce with `int()`.
- Scroll parameters: 400-1000px per step, 90% downward / 10% small upward jitter, 800-2500ms pause between steps (longer pauses let Facebook's lazy-load complete).
- The first 3 scrolls should always go down to get past sponsored listings quickly.

### Listing Descriptions — The Gap (March 30, 2026)

**Problem**: Facebook's GraphQL search API does NOT return `redacted_description` in search results — only in detail page queries (`CometMarketplaceItemDetailQuery`). This means:
- All listings extracted via anonymous GraphQL search have **empty descriptions**
- All listings extracted via DOM scraping have **empty descriptions** (DOM cards don't show descriptions)
- The ONLY way to get descriptions is via detail page visits

**Root cause found (March 30, 2026): browser-use's `Page.evaluate()` returns JSON strings, not Python objects.**

browser-use's CDP-based `Page.evaluate()` returns `json.dumps(value)` for dicts/lists and `''` for None. Every `page.evaluate()` call in the detail extractor was receiving a JSON string like `'[]'` or `'{"title": "..."}'` instead of a Python list/dict. The code then treated these strings as truthy/falsy or tried `.get()` on them, which silently failed inside `except Exception: pass` blocks.

**The fix**: A `_parse_evaluate_result()` wrapper that `json.loads()` the string back to a Python object. Applied to all `page.evaluate()` calls in `detail_extractor.py`.

**Result**: `enriched=8/9`, `with_description=6/13` on first fixed run (was `enriched=0` for weeks).

**Key finding: Facebook embeds listing data in data-sjs HTML, NOT separate XHR.**
CDP network interception of `/api/graphql/` responses does NOT capture listing detail data — Facebook includes it in the initial page HTML via `<script type="application/json" data-sjs>` tags. Separate XHR GraphQL calls on detail pages are only platform queries (backup, messenger, screen_time, etc.). The `DetailGraphQLInterceptor` (in `browser/detail_interceptor.py`) remains available but is not in the active extraction path.

**Current extraction architecture (three-tier)**:
- Tier 1: data-sjs script tag parsing — **PRIMARY, WORKING** — extracts title, description, price, creation_time, condition, seller, photos, GPS from ScheduledServerJS payloads
- Tier 2: DOM structural selectors — fills gaps (price text, location, freshness badge)
- Tier 3: Page markdown text parsing — last resort for freshness + seller info

**Mitigations for remaining no-description listings**:
1. VLM prompt includes `[No description available]` note when description empty, directing VLM to check photos
2. Stage 3 logging reports `with_description=N` count for observability
3. Backlog recovery filters garbage (title < 3 chars, FB UI artifacts) and permanently marks them evaluated
4. Misleading listing detection works on enriched listings that DO have descriptions

### Rate Limiting & Concurrency
- GraphQL rate limit: 6 seconds between requests (was 12, reduced after testing — no 429s observed at 6s).
- GraphQL searches can run concurrently (2 at a time via semaphore) because they're anonymous HTTP POSTs — no browser fingerprint linking them.
- Browser DOM searches must be serial — Facebook's bot detection tracks concurrent tab patterns.
- Inter-search stealth delay (8-12s) is only needed when the browser was used (DOM fallback). GraphQL has its own built-in rate limiting — adding extra delays doubles sweep time for no benefit.
- Detail page enrichment: 2 concurrent tabs is safe for a single logged-in account. 3+ risks detection.

### Timing Benchmarks (9 watchlist items, ~13 search configs)
- **Before optimization**: ~10.5 min (serial GraphQL with 12s delay + 10s inter-search delays)
- **After optimization**: ~3.5 min (concurrent GraphQL with 6s delay, no inter-search delay for GQL-only)
- Detail enrichment of 50 listings: ~4 min at 2-tab concurrency

---

## Price Handling — Critical Lessons

### `listing.price = None` is NOT `$0`
- A missing price means our scraper couldn't extract it. It does NOT mean the item is free.
- Previously, `listing.price or 0` converted None to 0, then `(MSRP - $0) / MSRP = 100%` → every unknown-price listing looked like an "incredible deal".
- This caused 29 fake deal notifications in a single patrol cycle.

### The Correct Architecture for Unknown Prices
Each layer handles unknown prices differently based on what it CAN do:

1. **VLM evaluation**: Works normally. The VLM sees the listing photo which literally shows the price. The prompt tells it "Not listed (check the listing image for the price)" so it reads the price visually.
2. **Programmatic savings enforcement** (`_enforce_dollar_savings`): **Skipped** when `listing.price is None`. We can't calculate dollar savings without a programmatic price. The VLM's assessment stands on its own.
3. **Score capping**: Unknown-price listings are capped at GREAT (never INCREDIBLE) to prevent spam when we can't verify savings with hard numbers.
4. **MSRP ratio check**: When listing price is unknown, the MSRP is kept for informational display but not used for ratio validation (can't divide by unknown).

### Free Items vs Unknown Prices
- `listing.price = 0` AND `listing.price is not None` → genuinely free, 100% discount is correct
- `listing.price = None` → unknown, let VLM assess, skip programmatic enforcement
- Always use `listing.price is not None` checks, never `listing.price or 0.0`

### GraphQL Price Extraction
Facebook uses different price field names across endpoints:
- `listing_price.amount` (most common)
- `price.amount`
- `formatted_price.text`
- `amount_with_offset_amount` (alternate sub-key)
- Price can be a dict, string ("$25"), or number
- The parser tries all known paths in priority order

---

## VLM & Triage Pipeline Design

### Watchlist Bypass
- Watchlist-tagged listings (`_watch_item_id` in raw_data) bypass text triage entirely.
- Reason: the triage LLM was observed killing 77% of valid watchlist matches. The user explicitly asked for these items — triage is too aggressive for them.
- Preference constraints (negation like "not metal", bulk detection, "only cups") are enforced programmatically by `_pre_enrichment_preference_filter` after the bypass, so no constraint enforcement is lost.

### VLM Prompt Price Display
- When price is known: `"Listed Price: $25.00"`
- When price is explicitly free: `"Listed Price: FREE"`
- When price is unknown: `"Listed Price: Not listed (check the listing image for the price)"`
- Previously showed "FREE" for unknown prices, which caused VLMs to hallucinate 100% discounts.

### Savings Enforcement Thresholds (Non-Negotiable)
These are programmatic overrides that VLM cannot bypass:
- **GOOD**: 15%+ discount AND $10+ saved
- **GREAT**: 30%+ discount AND $30+ saved
- **INCREDIBLE**: 50%+ discount AND $75+ saved
- Only applied when `listing.price is not None` (verified price available)
- When price is unknown, VLM score stands but capped at GREAT max

#### Price-Scaled Dollar Thresholds (for cheap items < $50)
The flat dollar thresholds ($10/$30/$75) are designed to prevent VLMs from rating cheap junk as "incredible" just because the percentage is high. But they over-penalize legitimate budget items — a $20 item at 50% off ($10 saved) is genuinely good, but fails the $30 GREAT threshold.

**Fix**: For items under $50, dollar thresholds scale proportionally:
- `effective_min = min(flat_threshold, listing_price * pct_factor)`
- GOOD: 15% of listing price (e.g., $3 for a $20 item vs $10 flat)
- GREAT: 25% of listing price (e.g., $5 for a $20 item vs $30 flat)
- INCREDIBLE: 40% of listing price (e.g., $8 for a $20 item vs $75 flat)
- Items $50+ use flat thresholds exclusively
- Items with unknown price ($0) use flat thresholds (conservative)

This means a $15 kitchen item at 40% off ($6 saved) now qualifies as GOOD (proportional min = $2.25) instead of being demoted to FAIR (flat min = $10).

---

## Facebook GraphQL Anonymous Client

### Session Bootstrapping
1. GET `facebook.com/marketplace/` to receive `datr` cookie + extract `lsd` token from HTML
2. POST `/api/graphql/` with `__user=0` (anonymous viewer), `datr` cookie, `lsd` token
3. This eliminates user-history personalization — results based purely on location/query/recency

### Response Format Quirks
- Facebook often returns GraphQL JSON with `text/html` content-type — check the body, not the content-type header
- Responses prefixed with `for (;;);` (anti-XSSI) — strip before parsing
- Empty `edges:[]` with a valid page_info cursor means "no more results for this query" — not an error, not a session problem
- When the response has `errors` in the JSON, the doc_id is likely stale and needs to be updated from browser DevTools

### doc_id Management
- `doc_id` values are pre-registered query IDs that Facebook maps to server-side queries
- They change infrequently but can break on redeploy
- Current working ID: `7111939778879383` (MARKETPLACE_SEARCH_DOC_ID)
- When broken: valid HTTP 200 but JSON contains `errors` array
- To get fresh doc_ids: open browser DevTools → Network → filter for `/api/graphql/` → search marketplace → copy `doc_id` from the POST body

---

## Listing Freshness Verification

### Timestamp Sources
- **GraphQL `creation_time`**: Unix timestamp, reliable, parsed into `posted_at` datetime
- **DOM freshness badge**: "Just listed", "Listed 3 hours ago", etc. Parsed by `_parse_freshness()` into approximate datetime
- **Detail page JSON-LD**: Sometimes has creation date, extracted during enrichment

### The No-Timestamp Gap
- DOM-scraped listings without a freshness badge get `posted_at=None`
- These are kept (can't verify age, but dedup prevents re-evaluation)
- With GraphQL as primary source, most listings now have timestamps
- The `no_timestamp` count in freshness filter logs shows how many slipped through

### Age Distribution Logging
Each patrol cycle logs:
```
Listing age distribution  timestamped=45  min_hours=0.2  max_hours=4.8  avg_hours=2.1
                          within_cutoff=43  stale=2  fb_filter_violations=1
```
- `fb_filter_violations` = listings older than `daysSinceListed * 24h` that Facebook served despite the filter
- This is proof that Facebook ignores its own age parameters

---

## Search Provider Cascade

### Current State (March 2026)
1. **Tavily**: Free tier exhausted (1,000/month limit hit)
2. **Serper.dev**: Free credits exhausted (2,500 one-time)
3. **SearXNG**: Self-hosted Docker, unlimited. Now handling all search traffic.
   - Runs as a service in `deploy/compose.yml` alongside scraper + ollama (single Komodo stack)
   - On homelab: scraper reaches it via service-name DNS — set `SEARXNG_BASE_URL=http://searxng:8080` in the Komodo stack env
   - Local dev: `docker compose -f deploy/compose.yml up -d searxng` exposes `http://localhost:8080` for the natively-running scraper; default `searxng_base_url` in `config.py` already points there, no override needed
   - Gracefully skips if container not running (ConnectError → unavailable)

## Wake-word model path on homelab

`hey_poob.onnx` is baked into the Docker image at `/app/hey_poob.onnx` (NOT `/app/data/`). `/app/data` is a volume mount in `deploy/compose.yml` and anything baked into that path gets shadowed by the empty named volume at runtime. When deploying on homelab, the Komodo stack env must set `PORCUPINE_KEYWORD_PATH=/app/hey_poob.onnx` (absolute path). Local dev keeps `PORCUPINE_KEYWORD_PATH=data/hey_poob.onnx` since no volume mount is in play.

openwakeword's preprocessor models (`melspectrogram.onnx`, `embedding_model.onnx`) don't ship with the pip package — they're downloaded on first use. The Dockerfile pre-fetches them at build time via `python -c "import openwakeword.utils; openwakeword.utils.download_models([])"` so the container has everything baked in and can start offline. Passing `[]` skips the pretrained hot-word models we don't use (alexa/hey_jarvis/etc.) — saves ~50 MB vs the default download.

---

## Production Log Access (for Agents)

Any agent working in this repo has read access to live container logs on homelab. The capability is wired through `scripts/logs.sh` — see CLAUDE.md for the quick reference. This section explains how it works so agents can debug the *log access path itself* if it breaks, and so future contributors understand what's underneath the wrapper.

### How the path works end-to-end

1. **Tailscale MagicDNS** resolves the hostname `homelab` to its tailnet IP (`100.125.74.35` in the `tail59ce07.ts.net` tailnet). Works from any device with Tailscale running and authed to the same tailnet (Ben's Google account).
2. **SSH config** on the dev PC has a `Host homelab` entry pointing at user `ben` with key auth via `~/.ssh/id_ed25519` (set up Apr 19 2026). Password auth is disabled on the homelab side (`/etc/ssh/sshd_config.d/99-hardening.conf`), so the key is the only way in.
3. **Docker socket on homelab** is owned by the `docker` group; user `ben` is in that group, so `docker logs` runs without sudo.
4. **`scripts/logs.sh`** is a thin wrapper that just shells out: `ssh homelab "docker logs <args> <container>"`. It's intentionally not magic — any flag `docker logs` accepts is forwarded.

If the wrapper fails, run the underlying command directly and bisect: `ssh homelab "docker ps"`, then `ssh homelab "docker logs --tail 50 poob"`. If `ssh homelab` itself fails, the issue is upstream (Tailscale not running, SSH key not loaded, key revoked).

### Log retention limits

Per-container Docker log rotation is configured globally in `/etc/docker/daemon.json` on homelab as `max-size: 50m`, `max-file: 5` — so each container keeps roughly 250 MB of rolling logs. Practical implications:

- High-volume containers (poob during active patrol) may rotate within hours.
- Quiet containers (poob-searxng, ollama between requests) keep weeks of logs.
- `--since` queries beyond the rotation horizon return nothing without warning. If an agent gets an empty response, increasing the window won't help — that data is gone.

For deploy-event history beyond log rotation (when an image landed, who triggered the deploy, env-var change history), use the **Komodo Updates panel** at `http://homelab:9120` → Stacks → poob → Updates. That metadata is stored in MongoDB and persists indefinitely.

### When SSH alone is the right tool

`scripts/logs.sh` is just for log queries. For other production operations, agents should use SSH directly:

- `ssh homelab "docker ps"` — what's running
- `ssh homelab "docker stats --no-stream"` — current resource usage
- `ssh homelab "docker exec ollama ollama list"` — what models are loaded
- `ssh homelab "docker compose -f ~/apps/komodo/docker-compose.yml ps"` — komodo health

Anything that mutates state (restarts, redeploys, env-var changes) should go through the Komodo UI, not raw `docker` commands — Komodo tracks those as Update events for audit history.

---

## SerpAPI
- Free tier: 250 searches/month
- Gets rate-limited quickly during patrol cycles with many listings
- Disabled for session when first 429 is received

---

## Browser Stealth

### What Facebook Tracks
- Viewport size (randomized via `stealth_viewport_randomize`)
- Mouse movements (simulated via `simulate_mouse_movement`)
- Scroll patterns (realistic: mostly down, occasional small up corrections)
- Request timing (randomized delays between page navigations)
- Concurrent tab patterns (limited to 2 for detail enrichment)

### Shadow Ban Detection
- 3+ consecutive empty sweeps triggers suspected shadow ban warning
- Tracked by `_consecutive_empty_sweeps` counter in PatrolEngine
- Reset on any successful sweep

---

## CRITICAL: Detail Page Enrichment Returns Empty Data (March 2026)

### The Problem
ALL 37 new listings in a run had `og={}` and `ld={}` after detail enrichment. Zero descriptions, zero prices from JSON-LD, zero timestamps from datePosted. The OG/JSON-LD JavaScript extractors return empty objects for every single listing page.

### Root Cause (CONFIRMED by research, March 2026)
- **OG meta tags**: Only served to crawler user agents (Googlebot, Facebookbot). Regular browser UAs get an empty shell. Our `meta[property^="og:"]` query will ALWAYS return empty.
- **JSON-LD**: Does NOT exist on Facebook Marketplace pages. Facebook uses Open Graph protocol, not schema.org/JSON-LD. No `script[type="application/ld+json"]` will ever appear.
- **React/Relay SPA**: All listing data flows through GraphQL queries fired by the client JS. The initial HTML is a minimal shell: resource preloads, module definitions, CSS links.

### Impact
- **No descriptions** for any listing (triage and VLM work blind on text)
- **25/37 listings with no price** (JSON-LD was supposed to fill missing GraphQL prices)
- **32/37 listings with no timestamp** (JSON-LD datePosted doesn't exist)
- **"Just listed" garbage titles survive** (OG title doesn't exist for browser UAs)

### Required Fix: GraphQL Network Interception on Detail Pages
The ONLY reliable method is intercepting the GraphQL response that Facebook's own JS fires when loading a detail page. This response contains 100+ structured fields.

**Three-tier extraction:**
1. **Tier 1: CDP Network interception** — listen for `/api/graphql/` responses containing `MarketplaceListing` nodes. Fields: `marketplace_listing_title`, `listing_price.formatted_amount`, `redacted_description.text`, `condition`, `creation_time`, all photo URIs, seller info, GPS coords.
2. **Tier 2: Embedded Relay JSON** — parse `script[type="application/json"][data-sjs]` tags with regex for `marketplace_listing_title`, `formatted_amount`, etc.
3. **Tier 3: DOM structural selectors** — aria-labels, `[role="main"]`, `h1 span[dir="auto"]`, `img[src*="scontent"]`

**Wait strategy:**
- Wait for `[role="main"] img` to appear (10s timeout)
- Monitor network for GraphQL responses with marketplace data
- 3s buffer after last GraphQL response

**Do NOT attempt**: OG tags, JSON-LD, React fiber walking, `__RELAY_DEVTOOLS_HOOK__` (only exists with DevTools extension installed).

### data-sjs Timing (March 17 2026)
After replacing OG/JSON-LD with data-sjs extraction, the first run returned `enriched=0`. Root cause: Facebook detail pages load in stages:
- **0-1s**: HTML shell, empty `data-sjs` scripts
- **1-3s**: React hydration begins
- **3-6s**: Main GraphQL response arrives, `data-sjs` tags populated
- **6-8s**: Full page render

The initial implementation used a single extraction at 3s — too early. Fix: poll loop checking every 1s for up to 6s total (2s nav wait + 4 polls × 1s). Break early when marketplace data is found.

Diagnostic logging added: if poll loop finds nothing, log how many `data-sjs` tags exist and their sizes. This reveals whether the tags exist but don't contain marketplace keywords (need to broaden search terms) vs the tags don't exist at all (need to wait longer or use different approach).

If data-sjs consistently returns empty on authenticated detail pages, the next escalation is CDP Network.responseReceived interception — capturing the raw GraphQL response as it arrives over the wire, before React processes it.

### Enrichment Early Bail (March 18 2026)
After two runs of `enriched=0 failed=0` (7+ minutes wasted each time), added early bail: if the first 5 consecutive listings return no new data, stop enrichment and log a warning. This caps the wasted time at ~45s instead of 7 minutes.

---

## New Provider Integration Issues (March 18 2026)

### Google CSE: HTTP 403 (Not Quota, Not Enabled)
The Custom Search JSON API must be explicitly enabled at:
`https://console.cloud.google.com/apis/library/customsearch.googleapis.com`
An API key alone is not enough — the API must be enabled for that project. 403 = "API not enabled" or "key restricted to other APIs." Fix: mark 403 as session-exhausted (same as 429) to stop retrying.

### Together.ai: HTTP 402 "Credit limit exceeded"
Together.ai's "Llama-Vision-Free" model is NOT truly free without a payment method on file. With $0 credit limit, every call returns 402. Fix: treat 402 + "credit" as a rate limit error in VLM cascade. User needs to add a payment method (even with free model, Together requires it as anti-abuse).

### Mistral Pixtral: HTTP 422 `extra_forbidden`
LangChain's `ChatOpenAI` sends `max_tokens` in the request body, but Mistral's API rejects unknown/extra fields. Fix: use `langchain-mistralai` package (`ChatMistralAI`) which knows Mistral's parameter names. Fallback: `ChatOpenAI` without `max_tokens`.

### GraphQL Price Field Formats (from research)
```
listing_price.amount                    → DOLLARS as string ("18.00")
listing_price.amount_with_offset        → CENTS as string ("1800") — detail pages
listing_price.amount_with_offset_in_currency → CENTS as string ("1800") — search results
listing_price.formatted_amount          → Display string ("$18.00")
listing_price.currency                  → ISO code ("USD")
strikethrough_price.formatted_amount    → Original price if reduced
```
CRITICAL: `amount` is DOLLARS (not cents). `amount_with_offset*` is CENTS. Both are STRINGS, never numbers.

---

## Triage & VLM Context — What Each Stage Sees

### Text Triage LLM receives:
- Title, price ("$X", "FREE", or "Price not listed"), description (300 chars max), location, seller, condition
- Watchlist preferences as HARD RULES
- Batch of 5 listings per prompt
- **Does NOT receive:** images, comparable sales, enrichment data

### VLM receives (15+ data points):
- 2 listing images (first + last)
- Title, price, description (500 chars max), location, seller, posted date
- Seller-stated original price (regex-extracted from description)
- Comparable sales (eBay medians or MSRP)
- Visual enrichment (product name, brand, model, OCR text)
- Condition signals (regex-detected from text)
- Model numbers (regex-detected)
- Title quality score (0-1, penalizes ALL CAPS, emojis, spam)
- Freshness bonus
- Multi-item/bulk flag
- Triage signals (urgency, misspelling, scam)
- Watchlist context with BINDING preference constraints

### Critical: Price Display Consistency
The same price display logic MUST be used in triage, VLM, and anywhere price is shown to an LLM:
```python
if listing.price is not None and listing.price > 0: "$X.XX"
elif listing.price is not None and listing.price == 0: "FREE"
else: "Price not listed" / "Not listed (check listing image for price)"
```
Previously, triage and VLM both showed "FREE" for unknown prices, causing mass false positives.

---

## Detail Enrichment — BROKEN (OG/JSON-LD are Dead)

### The Problem (March 17 2026 deep audit)
ALL OG meta tags and JSON-LD extraction returns EMPTY for every listing.
- `og:title`, `og:description`, `og:image` → only served to crawler user agents
- `script[type="application/ld+json"]` → does NOT exist on Facebook pages
- Zero descriptions, zero prices, zero timestamps recovered from detail enrichment
- See `docs/research/fb-detail-page-extraction.md` for full research

### Second Bug: ON CONFLICT Clause Missing `description`
Even if enrichment DID get data, the database UPDATE ignores it:
```sql
-- listing_repo.py ON CONFLICT clause is MISSING description:
ON CONFLICT(site, external_id) DO UPDATE SET
    title = excluded.title,
    price = excluded.price,
    -- description = excluded.description,  ← MISSING!
    -- posted_at = excluded.posted_at,      ← ALSO MISSING!
```
Result: enrichment runs in-memory, logs success, but the database row keeps NULL description forever.

### Required Fix: GraphQL Network Interception on Detail Pages
The ONLY reliable extraction method is intercepting `/api/graphql/` responses during detail page navigation. Facebook's own JS fires `CometMarketplaceItemDetailQuery` which returns 100+ fields including full description, all photos, condition, timestamps, GPS coordinates.

Three-tier extraction strategy:
1. **CDP Network interception** — listen for GraphQL responses with `MarketplaceListing` nodes
2. **Embedded Relay JSON** — parse `data-sjs` script tags with regex
3. **DOM structural selectors** — `h1 span[dir="auto"]` for title, `[role="main"]` for content

### Old Approach (Timestamp Recovery) — Partially Superseded
The old approach relied on JSON-LD which doesn't exist. However, freshness badge parsing from the detail page DOM still works:
- `_parse_freshness()` converts "Listed 3 hours ago" → approximate datetime
- This remains the Tier 3 fallback for timestamps

---

## GraphQL Price Extraction — `amount_with_offset` Cents Bug

### The Bug
`graphql_interceptor.py` line ~209 treats `amount_with_offset_amount` (CENTS) identically to `amount` (DOLLARS):
```python
amount_str = str(
    price_obj.get("amount")                        # ← DOLLARS ✓
    or price_obj.get("amount_with_offset_amount")   # ← CENTS, NOT /100 ✗
    or price_obj.get("text", "")
)
```
When `amount` is absent and `amount_with_offset_amount` is present, a $18.50 item appears as $1850.

### Also Missing: `amount_with_offset_in_currency`
The code doesn't check this field at all. Facebook search results use this variant.

### Fix
```python
amount_str = price_obj.get("amount")
if not amount_str:
    cents_str = (price_obj.get("amount_with_offset_in_currency")
                 or price_obj.get("amount_with_offset_amount")
                 or price_obj.get("amount_with_offset"))
    if cents_str:
        amount_str = str(float(cents_str) / 100)
if not amount_str:
    amount_str = price_obj.get("text", "") or price_obj.get("formatted_amount", "")
```

---

## VLM Quota Management — Google IPM Limit

### The Hidden Limit
Google AI Studio has an **undocumented Images Per Minute (IPM) limit** on free tier (2-10 images/minute). This is SEPARATE from the RPM/RPD limits.

A burst of VLM evaluations (each with 1-2 images) triggers IPM circuit breaker, returning `RESOURCE_EXHAUSTED` even if RPM/RPD are fine.

### Current Behavior
- VLM cascade tracks daily limits only, NOT IPM
- A burst of 50 listings → 100+ images → triggers Google's IPM immediately
- Google providers get demoted, leaving only Groq Vision + Ollama
- Groq Vision (14,400 RPD) quickly gets rate-limited too → `429 RESOURCE_EXHAUSTED`
- After ~30 evaluations, ALL VLM providers are exhausted

### Fix Needed
- Add per-provider IPM (images per minute) tracking
- Use leaky bucket (NOT token bucket) — smooth constant rate, never burst
- Preemptive throttling at 85% of daily limit
- Per-cycle budget: `budget = rpd * 0.80 / expected_daily_cycles`

### VLM Calls Per Listing
- Minimum: 3 (voting panel)
- Maximum: 7 (3 voters + tiebreaker + 3 second opinion)
- **Each call sends 1-2 images** → 3-14 images per listing evaluation
- 50 listings × 6 images avg = 300 images → far exceeds IPM for Google free tier

---

## Audit Findings (March 2026)

### Issue: 155 no-timestamp listings in a single cycle
- **Root cause**: Anonymous GraphQL browse returns `creation_time` for keyword searches but not always for the general feed. Detail enrichment returns empty (OG/JSON-LD are dead).
- **Mitigation**: GraphQL interception on detail pages will provide `creation_time`
- **Monitoring**: `no_timestamp` count in freshness filter logs

### Issue: 29 false deals from Legos watchlist (first occurrence)
- **Root cause**: `listing.price or 0.0` treated None as $0 → 100% discount on everything
- **Fix**: Separated None (unknown) from 0 (free) throughout pipeline
- **Guard**: VLM score capped at GREAT for unknown-price listings

### Issue: Triage LLM saw "FREE" for unknown prices
- **Root cause**: Same `or` pattern as above in `_build_listings_block()`
- **Fix**: Three-way display: "$X", "FREE", "Price not listed"

### Issue: All 50 watchlist listings bypassed triage
- **By design**: Triage was observed killing 77% of valid watchlist matches
- **Safety net**: Preference constraints enforced programmatically after bypass
- **Guard**: Savings enforcement still applies when price is known

### Issue: 25/37 listings had NO price (deep audit, March 17 2026)
- **Root cause 1**: GraphQL `amount` field is absent for some listings; `amount_with_offset*` fields are NOT checked properly (cents bug or not checked at all)
- **Root cause 2**: Detail enrichment returns empty data (OG/JSON-LD dead)
- **Fix needed**: Fix cents conversion in graphql_interceptor.py, implement GraphQL interception on detail pages

### Issue: 0/37 descriptions saved to DB despite "34/34 enriched"
- **Root cause**: `listing_repo.py` ON CONFLICT clause missing `description` and `posted_at` in UPDATE SET
- **Fix**: Add `description = excluded.description` and `posted_at = COALESCE(excluded.posted_at, posted_at)` to upsert

### Issue: "cookies" watchlist matching baked goods sellers
- **Observation**: Watchlist item "cookies" matches "Easter Cookies", "Delicious Cupcakes", "Easter cupcakes"
- **These are actual food being sold by bakers**, not cookie-themed items/collectibles
- **Mitigation options**: Add notes like "only cookie jars, cookie cutters, vintage cookie items — not actual food/baked goods" to watchlist item

### Issue: VLM cascade exhaustion after ~30 evaluations
- **Root cause**: Google's IPM limit (undocumented), Groq's 30 RPM → both exhausted mid-cycle
- **Symptom**: "All 8 VLM providers exhausted" for final 20 listings
- **Fix needed**: IPM tracking, leaky bucket, per-cycle budget allocation, more free VLM providers (Together.ai, Mistral, Cloudflare)

### Issue: Early exit voting causes false demotions
- **Root cause**: When 2/3 VLM voters agree and the 3rd task is cancelled, the cancellation increments the consecutive failure counter
- **Symptom**: Healthy providers get demoted for 10 minutes because of successful early exits
- **Fix**: Don't count task cancellations as failures in the demotion system

---

## Common Pitfalls — Read Before Changing

1. **Never treat `None` as `0` for prices.** Use `is not None` checks. `listing.price or 0.0` is a bug that caused 29 false deals in one cycle.
2. **GraphQL empty responses are normal.** `edges:[]` with `text/html` content-type is "no results", not "blocked". Don't retry or re-bootstrap.
3. **Stealth delays are only for browser actions.** GraphQL HTTP requests have their own rate limiting — don't add inter-search delays on top.
4. **`page.evaluate()` can return strings.** Always coerce numeric results with `int()` or `float()`.
5. **The VLM can see the price in photos.** Don't assume unknown price = unknown deal quality. The VLM has more context than our scraper.
6. **Savings enforcement is the safety net, not the evaluator.** It only runs when `listing.price is not None`. When price is unknown, VLM assessment stands (capped at GREAT).
7. **Facebook's sort order is a suggestion.** Always verify with `_verify_sort_order` and expect violations — engagement-promoted listings get injected.
8. **Price display must be consistent across ALL LLM prompts.** Triage, VLM, and any future LLM-facing code must use the same three-way logic (known/free/unknown).
9. **Detail enrichment never overwrites existing good data.** Price, posted_at, and other fields are only filled if currently None.
10. **Watchlist items bypass triage for a reason.** The triage LLM killed 77% of valid watchlist matches. Don't re-add triage for watchlist items without measuring the impact.
11. **OG tags and JSON-LD are dead on Facebook.** Do NOT add OG/JSON-LD extraction — it returns empty for ALL listings. Use GraphQL network interception instead. See `docs/research/fb-detail-page-extraction.md`.
12. **`amount_with_offset*` fields are CENTS, not dollars.** Always divide by 100 before storing as price. `amount` is dollars. Both are strings.
13. **Database upsert must include ALL enriched fields.** If a field isn't in the ON CONFLICT UPDATE SET, enriched values are silently discarded. Check listing_repo.py when adding new extractable fields.
14. **Google has an undocumented Images Per Minute limit.** Free tier allows ~2-10 images/minute. Bursting VLM calls exhausts this before daily RPD is reached. Use a leaky bucket to smooth image submission.
15. **VLM task cancellation ≠ provider failure.** The voting system's early exit cancels the 3rd provider. Don't increment failure counters on cancelled tasks — it causes false demotions of healthy providers.
16. **Never append raw web search text to VLM reasoning.** Web search runs in parallel with VLM for latency, but the results are often garbage (wrong product matched by search engine). The `[Web: ...]` append was removed — web search usage is tracked in `DealProvenance` instead.
17. **Replacement parts filter.** Watchlist search for "kitchen aid" matches $5 gaskets/valves. `_is_replacement_part()` catches these via part number patterns + repair keywords. Only applied to watchlist listings where the interest isn't explicitly a "part".

---

## Deal Provenance Trail (March 2026)

Each deal notification now includes an "Evaluation Details" section showing which models/services contributed to the decision:

### Data Flow
- `DealProvenance` dataclass accumulates state through the pipeline stages
- Triage → enrichment tier → price source → VLM providers/confidence → score adjustments
- Serialized to JSON and stored in `deals.provenance_json` column
- Rendered as compact text in Discord embed ("Evaluation Details" field)

### Key Design Decisions
- **Single flat dataclass** with stage prefixes (`vlm_*`, `price_*`) — not per-stage objects
- **VLMCascade exposes `last_responding_providers`** — set after each invoke()
- **Web search results captured in provenance, NOT in reasoning** — prevents garbage in notifications
- **Score adjustments recorded as strings** — e.g., "incredible->great: $28 saved, 55%"
- **Backward compatible** — old deals without provenance_json show no "Evaluation Details" field

## Unified PoobBrain Architecture (March 2026)

### Problem: Two Disconnected Personalities
Previously, Poob had two completely separate brains:
1. **Text agent** (`agent/runner.py` + `agent/prompts.py`): "dry sarcastic deal assistant" personality, 3KB+ system prompt, 14 tools, LangChain tool-calling loop via Groq GPT-OSS 120B cascade.
2. **Voice** (`voice/conversation.py`): "loud, bold, chaotic" personality, ~500 char prompt, zero tools, Groq 8B via SDK directly.

These shared no conversation history, personality, or capabilities. Voice-Poob couldn't help with deals at all.

### Solution: Thin Personality Router + Deal Sub-Agent

**PoobBrain** (`brain/poob.py`) is now the single entry point for ALL Poob interactions (text, DMs, voice). It uses the **agent-as-tool** pattern:

```
User Input (any source)
    │
    ▼
PoobBrain (tiny ~500 char prompt + deal_assistant tool)
    │
    ├── No tool called → casual personality response (Groq, ~200ms)
    │
    └── deal_assistant called → AgentRunner (heavy prompt, 14 tools)
                                │
                                └── Result wrapped in Poob personality
```

### Key Design Decisions

- **LLM IS the router**: No separate intent classifier. Groq's native function calling decides whether to route to the deal agent. The `deal_assistant` tool description covers all deal/shopping-related requests.
- **Deal agent has no personality** (`personality=False`): AgentRunner's system prompt uses `_SUB_AGENT_PREAMBLE` — clean, functional, no humor. PoobBrain wraps the response in personality after.
- **Data-heavy passthrough**: When the deal agent returns structured data (>500 chars, contains newlines — tables/lists), PoobBrain passes it through without personality wrapping to preserve formatting.
- **Multi-turn deal sessions**: `_deal_context` dict tracks active deal sessions per user. When the deal agent asks follow-up questions (response contains "?"), context is preserved so the LLM routes subsequent answers back to the deal agent.
- **Voice streaming**: `respond_streaming()` checks for tool calls first (non-streaming, fast), then streams the personality wrap sentence-by-sentence for TTS.
- **Shared conversation history**: PoobBrain maintains one in-memory history per user, shared across text and voice. The deal agent maintains its own DB-backed history for tool-calling continuity.
- **Channel context for text channels**: `AgentMessageHandler` fetches the last ~15 messages from the Discord channel via `channel.history()` and formats them as an attributed transcript (same `[Recent conversation you've been listening to:\n...]` format voice uses). This is injected into the *current* LLM turn only — `_split_context()` separates the context block from the clean user text before storing in history, preventing history bloat. This means Poob always knows what was just said in the channel, even when the user sends a bare `@Poob` mention with no text.

### Latency Profile
- **Casual text**: ~200-400ms (Groq single call, no tools)
- **Casual voice**: ~500ms total (STT + Groq + TTS)
- **Deal text**: ~1-3s (Groq routing + deal agent + personality wrap)
- **Deal voice**: ~2-4s (STT + Groq routing + deal agent + streaming wrap + TTS) — filler audio covers the gap

### Files Changed
- **NEW**: `brain/__init__.py`, `brain/poob.py` — unified personality layer
- **MODIFIED**: `agent/prompts.py` — added `_SUB_AGENT_PREAMBLE`, `personality` param on `build_system_prompt()`
- **MODIFIED**: `agent/runner.py` — added `personality` param (defaults True for backward compat)
- **MODIFIED**: `discord_bot/agent_handler.py` — routes through PoobBrain instead of AgentRunner
- **MODIFIED**: `discord_bot/bot.py` — `poob_brain` replaces `agent_runner`
- **MODIFIED**: `voice/session.py` — `brain: PoobBrain` replaces `conversation: VoiceConversationManager`
- **MODIFIED**: `discord_bot/cogs/voice_cog.py` — text-to-voice uses `session.brain.respond()`
- **MODIFIED**: `main.py` — creates PoobBrain, passes to voice sessions and bot
- **DEPRECATED** (not deleted): `voice/conversation.py` — VoiceConversationManager no longer used in production

### Common Pitfalls
- **Don't add personality to the deal agent prompt**: PoobBrain handles personality. If the deal agent has personality too, you get double personality (weird).
- **Data-heavy responses should not be personality-wrapped**: Tables, lists, deal details lose formatting if run through a personality LLM. The >500 char / newline heuristic handles this.
- **Voice deal sessions add ~2s latency**: Filler audio infrastructure exists (`voice/fillers.py`) but isn't yet triggered during deal routing. Future work: play filler while deal agent runs.
- **8B models may struggle with routing**: If `llama-3.1-8b-instant` fails to call `deal_assistant` for obvious deal requests, switch to `llama-3.3-70b-versatile` (still fast on Groq).
- **Channel context must be ephemeral**: The `[Recent conversation...]` block is injected into the LLM prompt for the current turn only. `_split_context()` in `poob.py` strips it before saving to per-user `_histories`. Without this, every history entry would contain 15 lines of channel context, ballooning token usage across turns.
- **Bare mentions default to context inference**: When a user sends just `@Poob` (no text), content becomes `"(responding to the conversation above)"` instead of `"hi"` — but only when channel context is available. This lets the LLM infer intent from what was just said.

---

## VLM Cascade — Operational Findings (March 29, 2026)

### Provider Performance (Observed)
| Provider | Avg Response | Reliability | Notes |
|----------|-------------|-------------|-------|
| Groq Vision (Llama 4 Scout 17B) | 0.5-1.5s | Excellent (14,400 RPD) | Best speed, adequate quality |
| Gemini Flash Lite | 1.5-3s | Good (1,000 RPD) | Consistent, high volume |
| Gemini Flash | 2-6s | Good but IPM-limited (250 RPD) | Hits RESOURCE_EXHAUSTED after ~8 evals |
| Mistral Pixtral 12B | 2-3s | Good (1,400 RPD) | Stable, predictable |
| Gemma 27B | 5-12s | Unreliable | ALWAYS cancelled in voting (too slow). Hits RESOURCE_EXHAUSTED frequently. Only useful as fallback when faster providers are exhausted. |

### Dead/Permanent Error Providers
- **Together.ai**: Returns 402 "Credit limit exceeded" — no free tier without payment method. Auto-disabled for 24h on first failure.
- **OpenRouter Mistral Small 3.1 24B**: Returns 404 "No endpoints found" — model removed from OpenRouter. Auto-disabled for 24h on first failure.
- **OpenRouter Nemotron**: Often timeout/cancelled. Low priority fallback.
- **Ollama VLM**: Requires local Ollama running. Emergency fallback only.

### Cascade Order Rationale (current)
1. Gemini Flash — highest quality free VLM
2. Gemini Flash Lite — high RPD, fast, good quality
3. Groq Vision — very fast, highest RPD, adequate quality
4. Mistral Pixtral — stable, different architecture (diversity for voting)
5. Gemma 27B — slow but Google-diverse; only used when top 4 exhausted
6-10. Fallbacks (Together, OpenRouter, Gemini Pro tiebreaker, Ollama)

### Permanent Error Detection
The cascade now distinguishes transient rate limits (429, RESOURCE_EXHAUSTED) from permanent errors (402 billing, 404 model not found, 401 auth). Permanent errors disable the provider for 24h (session lifetime), eliminating wasted panel slots on every eval.

### Voting Consensus
`no_consensus` is frequent (~40% of evals). The first responder's opinion is used when 3 providers disagree. This means Groq Vision's opinion dominates (it's fastest). This is acceptable — Groq's accuracy is adequate for deal detection, and the alternative (blocking on slow tiebreaker calls) would double evaluation time.

---

## Garbage Listing Filter (March 29, 2026)

### Problem: Empty/Placeholder Titles Reaching VLM
DOM extraction sometimes captures Facebook UI elements or freshness badges instead of real listing titles. When detail enrichment fails (enriched=0, as is currently the case), these survive all the way to VLM evaluation.

### Categories
1. **UI artifacts**: "See details", "Loading", "Facebook Marketplace" — Facebook's own UI text
2. **Freshness badges**: "Just listed", "Listed today", "" (empty) — DOM scraper captures the freshness indicator instead of the actual title
3. **Partial extraction**: Very short fragments from CSS-clipped text

### Fix: Post-Enrichment Garbage Filter
The `GarbageFilter` in `listing_filter.py` (formerly `_filter_garbage_listings` in patrol_engine.py) catches both categories:
- UI artifacts: Always removed (no recovery possible)
- Unenrichable placeholders: Removed ONLY if description is also < 20 chars. If description is present, VLM can still reason from description + image.

### Why Not Filter Earlier?
These listings are NOT filtered pre-enrichment because detail enrichment is designed to fix them (visit the listing page → extract real title). Only after enrichment has had its chance and failed do we discard.

---

## Music Bot Integration (April 2026)

### Architecture: yt-dlp + FFmpeg + audioop PCM Mixer (No Lavalink)

**Decision:** Direct yt-dlp + FFmpeg instead of Lavalink. Rationale: single-server bot, no JVM overhead (300-500MB RAM saved), FFmpeg already a dependency, and we need raw PCM access for the TTS mixing pipeline. Lavalink's seeking/filters advantages don't apply — we need mixing, which Lavalink doesn't support natively (only NodeLink does).

**Key files:**
- `src/poob/music/queue.py` — Track dataclass, MusicQueue with loop/shuffle
- `src/poob/music/ytdl.py` — AsyncYTDL wrapper (all yt-dlp in ThreadPoolExecutor) + pre-download
- `src/poob/music/player.py` — MixingAudioSource + BufferedAudioSource + GuildMusicPlayer
- `src/poob/discord_bot/cogs/music_cog.py` — Commands + agentic handler

### Audio Pipeline — Four-Layer Anti-Stutter Architecture (April 2026)

Choppy audio had four independent causes, each fixed at the right layer:

**Layer 1 — Pre-download (eliminates YouTube TLS termination):**
YouTube CDN kills TLS sessions after ~3-4 minutes (yt-dlp #8854). FFmpeg reconnect can't fully recover because the URL may be expired. Fix: `AsyncYTDL.download_track()` pre-downloads audio to a temp file before playback. FFmpeg reads from local disk — zero network dependency during playback. Livestreams fall back to streaming with reconnect flags. Temp files are cleaned up after each track finishes or on stop/destroy.

**Layer 2 — BufferedAudioSource (absorbs I/O jitter):**
Pycord's audio thread calls `read()` every 20ms. If FFmpeg's pipe blocks, the frame is late. `BufferedAudioSource` wraps FFmpegPCMAudio with a dedicated reader thread filling a 100-frame (2-second) queue. The audio thread reads from the queue (always fast), never from the pipe. Returns silence on underrun (not empty bytes — empty signals EOF to Pycord). Prefills 10 frames (200ms) before playback starts.

**Layer 3 — audioop mixer (eliminates per-frame allocations):**
Replaced numpy with `audioop` (C extension, `audioop-lts` on Python 3.13+). `audioop.mul()` for volume scaling, `audioop.add()` for mixing — both operate directly on bytes with built-in int16 clipping. Zero per-frame allocations, ~5-10x faster than numpy for 1920-sample buffers. Eliminates GC pressure that caused frame drops.

**Layer 4 — Python 3.13 timer resolution:**
Python 3.13 uses `CREATE_WAITABLE_TIMER_HIGH_RESOLUTION` on Windows — ~100ns sleep accuracy vs the old 15.6ms default. No code changes needed, just requires Python 3.11+.

### MixingAudioSource — PCM Mixer

Discord allows exactly ONE audio stream per bot per guild. `VoiceClient.play()` raises `ClientException` if called while playing. Our solution: a custom `discord.AudioSource` that mixes music + TTS in real-time using audioop.

**How it works:**
1. Music plays through `MixingAudioSource` as the primary source
2. When Poob needs to speak, TTS audio is injected as an "overlay" via `play_overlay()`
3. The mixer's `read()` method (called 50x/sec by Pycord) reads both sources, ducks music to 25% volume via `audioop.mul()`, sums with `audioop.add()` (built-in int16 clipping), returns mixed frame
4. When TTS ends (grace period of 8 empty reads = 160ms), music volume ramps back up

**Critical details:**
- **audioop handles clipping** — `audioop.add(a, b, 2)` clips int16 overflow internally in C, no manual clip step needed
- **Gain ramps** — 15 frames (300ms) fade prevents audible click/pop on volume changes
- **Grace period** — 8 empty overlay reads before cleanup, prevents premature TTS kill during FFmpeg buffering
- **Frame size** — exactly 3840 bytes per read() (20ms at 48kHz stereo 16-bit), enforced by Pycord
- **Thread safety** — `read()` runs in Pycord's audio daemon thread; overlay injection from asyncio thread is safe because Python GIL makes single-pointer writes atomic

### yt-dlp: Pre-Download + Streaming Fallback

**Primary path — pre-download to temp file:** `download_track()` downloads audio-only to a temp `.webm` file (2-5 seconds for typical tracks). FFmpeg reads from disk — immune to CDN jitter, TLS termination, and URL expiration. Download latency is hidden by the pre-fetch system: next track downloads while current plays. Temp files cleaned up after playback.

**Fallback — stream via URL:** For livestreams (`track.is_stream`) or download failures, falls back to `resolve_stream_url()` + network streaming with full FFmpeg reconnect flags. Both paths go through `BufferedAudioSource` so even streaming benefits from the read-ahead buffer.

**FFmpeg flags (streaming path):** `-nostdin -probesize 1000000 -analyzeduration 0 -reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 -reconnect_delay_max 5`. The `-probesize 1000000 -analyzeduration 0` eliminates the initial burst-then-stall pattern. `-reconnect_on_network_error 1` handles TCP/TLS resets. `-nostdin` prevents FFmpeg from blocking on stdin (Windows issue).

**FFmpeg flags (local file path):** Just `-nostdin`. No reconnect, no probe tuning — disk reads are instant.

**URL expiration:** YouTube stream URLs expire ~6 hours. Queue stores permanent `webpage_url`. Pre-download eliminates this concern for most tracks. Stream URL resolved only as a fallback.

**Playlists:** `extract_flat: 'in_playlist'` mode grabs only metadata (title, ID, duration) without resolving stream URLs — turns a 60-second 50-track extraction into 2 seconds.

**Search:** `ytsearch1:query` via `default_search: 'auto'` handles both URLs and text queries through the same code path.

**All yt-dlp calls run in ThreadPoolExecutor** (3 workers, dedicated pool) — yt-dlp is synchronous and will block the event loop / kill Discord heartbeat if run on the main thread.

### Why FFmpegPCMAudio (Not FFmpegOpusAudio)

`FFmpegOpusAudio` is more CPU-efficient (Opus passthrough, no re-encoding) but is **incompatible with PCM mixing**. Our mixer needs raw int16 samples to sum. CPU cost of PCM decode→encode for one stream on modern hardware is negligible.

### Agentic Music Integration (Voice + Text Channels)

Music commands flow through the same PoobBrain tool-routing as deals. Three paths, one handler:

**Voice path:**
1. User speaks → STT → "hey Poob play some chill beats"
2. Wake word detected → PoobBrain receives message
3. Groq 70B with `music_assistant` tool → tool call detected
4. `MusicCog.handle_music_request()` parses intent → searches YouTube → queues track
5. Response wrapped in Toob personality → TTS → injected as overlay on music
6. Music ducks → Toob speaks → music restores

**Text channel path (agentic — @mention):**
1. User types `@Poob play some chill beats` in any text channel
2. `AgentMessageHandler` passes `guild_id` + message to `PoobBrain.respond()`
3. Groq 70B with `music_assistant` tool → tool call detected
4. `_handle_music()` resolves guild via explicit `guild_id` (not `_voice_guild_id`)
5. `handle_music_request()` finds bot's voice client in guild, or auto-joins
6. Response wrapped in Poob personality → posted as text reply in channel
7. Control commands (skip/pause/etc.) return status text instead of `[SILENT]` empty

**Auto-join:** If the bot isn't in any voice channel when a text-channel music request arrives, `_auto_join_voice()` connects to the voice channel with the most human members. This is a lightweight join — no VoiceSession (STT/recording). Users `!join` for full voice interaction.

**Guild ID routing:** `brain.respond()` accepts `guild_id` from callers. Text channels pass `message.guild.id` explicitly. Voice sessions set `_voice_guild_id` during utterance processing. `_handle_music()` prefers the explicit guild_id, falls back to `_voice_guild_id`.

**`[SILENT]` protocol — voice vs. text:** Control commands return `[SILENT]Status message` from the music handler. In voice mode, the brain returns empty string (no TTS). In text mode, the brain strips the prefix and returns the status text so the user sees feedback like "Skipped Bohemian Rhapsody."

**Fallback text commands:** `!play`, `!skip`, `!queue`, `!pause`, `!stop`, `!shuffle`, `!loop`, `!volume`, `!np` — all delegate to the same `handle_music_request()`. The `!play` command no longer requires the user to be in a voice channel — `handle_music_request` handles auto-join.

### Queue Architecture

List-backed (not deque) — per Wavelink 3.x migration rationale: music queues need random access for display pagination, `random.shuffle()`, and remove-by-index. Performance of `pop(0)` on sub-500 queues is negligible.

**Loop modes:** OFF → LOOP_ONE → LOOP_QUEUE, all resolved in single `get_next()` method.

**Shuffle:** Fisher-Yates with saved original order. Unshuffle restores only remaining tracks.

**Transitions:** Event-driven via `asyncio.Event`. The `after` callback in `vc.play()` fires in FFmpeg's reader thread and calls `loop.call_soon_threadsafe(event.set)` to unblock the async player loop. No polling.

### Common Pitfalls

1. **Integer overflow in mixing** — `audioop.add()` handles clipping internally. The old numpy path required manual float32 upcasting + `np.clip()`. Don't regress to numpy.
2. **Instant volume changes click** — stepping from 100% to 25% in a single frame causes a transient pop. Gain ramps (300ms) fix this.
3. **yt-dlp on event loop** — blocks heartbeat, Discord disconnects. Always `run_in_executor`.
4. **Stale stream URLs** — YouTube URLs expire ~6 hours. Pre-download eliminates this for normal tracks. Stream URLs only used for livestream fallback.
5. **`play()` while playing** — raises `ClientException`. The mixer prevents this by being the sole source.
6. **Grace period on overlay** — cloud TTS may buffer; first few `read()` returns empty. Without grace period, mixer kills overlay before audio starts.
7. **BufferedAudioSource: silence vs empty bytes** — returning `b""` from `read()` tells Pycord the track ended. On buffer underrun, return silence (`b"\x00" * 3840`) to keep the stream alive while the buffer refills.
8. **Temp file cleanup** — `download_track()` creates temp files. They MUST be cleaned up after playback (`cleanup_track_file()`). The player loop, `stop()`, and `destroy()` all handle this. Forgetting cleanup causes disk exhaustion.
9. **yt-dlp download extension mismatch** — yt-dlp may change the output file extension (e.g., `.webm` → `.opus`). `download_track()` checks multiple candidates.

## Voice Architecture — Toob, Deferred Playback, Multi-Provider Cascade (April 7, 2026)

### Toob — Evil Music Spirit

**What:** When Poob handles a music *play/queue* command, his evil cousin Toob responds instead. Toob has a separate personality prompt and a deep, pitch-shifted warlord voice. Music *control* commands (skip, pause, stop, volume) execute silently — no TTS, no personality wrap.

**Voice signal routing (structural, not string-based):**
- `VOICE_TOOB = "__VOICE_TOOB__"` — a constant yielded as the FIRST item from `respond_streaming()` when Toob should speak
- Session detects this signal in its async iteration loop and switches the synth function: `synth = self._synthesize_toob if use_toob_voice else self._synthesize`
- **No string prefixes, no `[TOOB]` markers, no regex parsing** — the tool call itself is the routing signal

**Toob's voice pipeline:**
1. Google Chirp3-HD Enceladus voice at 0.95x speaking rate
2. FFmpeg warlord filter: `asetrate=16000,aresample=24000,atempo=1.7,bass=g=10:f=80,aecho=0.8:0.85:40:0.3`
   - `asetrate=16000` on 24kHz source → pitch DOWN (voice deepens)
   - `atempo=1.7` → compensates duration stretch + faster delivery
   - `bass=g=10:f=80` → heavy bass boost (rumble)
   - `aecho` → cavernous reverb
3. FFmpeg processing adds ~100-230ms latency (first run ~770ms cold start)
4. Fallback: if FFmpeg fails → raw Enceladus audio; if TTS fails entirely → regular Poob voice

**Why asetrate < native rate = pitch down:** FFmpeg interprets the source as 16kHz but it's actually 24kHz. When resampled to 24kHz output, the audio gets stretched (lower pitch). `atempo` then compensates the speed change so duration stays reasonable.

### Silent Music Controls

Control commands (skip, pause, resume, stop, volume, shuffle, loop) return `[SILENT]` prefix from the music handler. The brain detects this prefix, returns empty string to the session → no TTS generated, no personality wrap. The action executes instantly (~600ms end-to-end).

**Why `[SILENT]` is not a bandaid:** It's a structured protocol between the music handler and the brain. The handler decides which commands are silent (controls) vs. verbose (play/queue). The brain doesn't parse content — it checks a prefix on the handler's return value.

### Deferred Playback — Session-Level Orchestration

**Problem:** When a user says "play X" in voice, the music should start AFTER Toob finishes speaking, not immediately.

**Solution:** Pure state inspection in the session, no flags, no cross-layer coupling:
1. Music handler queues the track with `deferred=True` → URL pre-resolves but playback doesn't start
2. Toob responds → TTS plays → session checks: `music_player.is_playing == False AND queue not empty`
3. If true → `music_player.start_deferred()` → music begins

**Why not flags:** The previous `_music_deferred_pending` flag on PoobBrain was shared mutable state across all users/guilds — a race condition. The session now observes player state directly. No flags, no callbacks, no coupling between brain and session.

### Multi-Provider Tool-Calling Cascade

**Problem:** Groq 70B (primary tool-caller) hits 429 rate limits constantly in multi-user voice sessions. The fallback to Scout 17B misrouted almost everything to `music_assistant`.

**Benchmarked results (April 7, 2026):**

| Provider / Model | Play Latency | Skip Correct? | Tool Support |
|---|---|---|---|
| Groq llama-3.3-70b-versatile | 925ms | Yes | Full |
| Groq llama-4-scout-17b | 858ms | Yes | Full but over-aggressive |
| Cerebras qwen-3-235b | 752ms | Yes | Full |
| NVIDIA qwen3-next-80b | 930ms | Yes | Full |
| Groq llama-3.1-8b-instant | 518ms | ERR 400 | Partial (fails on short msgs) |
| Groq llama-3.3-70b-specdec | - | ERR 400 | Broken |

**Cascade order (in `_groq_with_tools`):**
1. Groq llama-3.3-70b-versatile (best accuracy)
2. Cerebras qwen-3-235b (different provider, avoids Groq rate limits)
3. NVIDIA NIM qwen3-next-80b (third provider)
4. Groq llama-4-scout-17b (last resort only)

Three different providers. If Groq is rate-limited, Cerebras picks it up immediately — no falling through to the weak Scout model.

### Music State Context in System Prompt

When music is playing, the system prompt is dynamically extended with:
```
[MUSIC IS CURRENTLY PLAYING: {title} [{duration}].
CRITICAL: When music is playing and the user says ANY of these,
you MUST call music_assistant: stop, skip, pause, resume, volume,
turn down, turn up, max volume, mute, next, shuffle, loop, what's playing...]
```

This gives the LLM the context to route "skip" correctly without keyword matching. Without music playing, "skip" is just a word. With music playing, the LLM understands it's a control command.

### Wake Word Stripping in Music Handler

The tool-caller passes the FULL user message including "Hey, Poob." to the music handler. Without stripping, YouTube searches for "Hey, Poob. Stop." and finds random videos.

Fix: regex strip at the top of `handle_music_request()`:
```python
cleaned = re.sub(r'^(?:hey[,.]?\s*)?(?:poob|poop|pub|boob)[,.]?\s*', '', request, flags=re.IGNORECASE).strip()
```

### Programmatic Personality Variance (90 POOB_STATES)

**Problem:** LLMs with monolithic personality prompts degrade into repetitive caricatures. The model sees its own intense responses in chat history and overfits to them (context compaction / echo chamber).

**Solution:** 90 random "vibe" states injected per-call at the code layer:
```python
state = random.choice(POOB_STATES)
prompt += f"\n\n[YOUR CURRENT VIBE (embody this in your tone, do NOT mention or describe it): {state}]"
```

Because the vibe changes every turn, even if chat history is full of paranoid responses, the new prompt might say he's mourning a dust mite — forcing a tone shift that feels organic.

**Key rules in the system prompt:**
- Answer what was asked FIRST, then let vibe color delivery
- 1-2 sentences max
- NEVER mention or describe the vibe — embody it in tone

### Wake Word Detection Improvements

1. **Threshold 0.5 → 0.7** — reduces false positives in multi-user calls
2. **Pending wake timeout 3s → 1.5s** — stale detections don't carry forward
3. **Dual confirmation: audio + text** — if audio model fires but transcript doesn't contain "Hey Poob", detection is overridden. Text regex is authoritative for positive matches; audio model is authoritative for rejection only.
4. **15s max utterance duration** — prevents 47-second accumulated transcripts from being treated as single commands
5. **Deepgram keyterms** — `play`, `skip`, `stop`, `pause`, `volume`, `shuffle` boosted for STT accuracy

### Common Pitfalls — Voice/Music

1. **`is_playing` is a property, not a method** — `player.is_playing()` crashes with `'bool' object is not callable`. Use `player.is_playing`.
2. **`yield from` in async generators** — syntax error. Use `for item in ...: yield item` instead.
3. **asetrate math is inverted from intuition** — `asetrate` BELOW native rate = pitch DOWN (not up). The filter reinterprets the source rate.
4. **Groq 70B rate limits constantly** in multi-user sessions — always have non-Groq fallback (Cerebras, NVIDIA).
5. **Scout 17B over-routes to music** — "Did you get offended?" → plays "Big Ole Freak". Keep it last in cascade.
6. **`<function=...>` in LLM output** — some models output tool calls as raw text instead of structured `tool_calls`. Regex cleanup in both `respond()` and `respond_streaming()` prevents markup from reaching Discord.
7. **Deepgram transcript replay** — `reset_transcript(user_id)` must be called after utterance emission, not just at utterance start.

---

## Unified Filter Pipeline (April 2026 Refactor)

### Problem: Bandaid Accumulation
The patrol pipeline had accumulated fragmented filtering logic:
- `_EXCLUDED_CATEGORY_PATTERNS`: 65+ hardcoded vehicle/housing keywords, growing endlessly
- `_ALLOWED_NOTIFY_STATES`: Hardcoded US state whitelist (WI, MN, IA, IL, MI, IN) — breaks for any other user
- Triple-check pattern: Same filter logic copy-pasted at pre-enrichment, post-enrichment, AND notification stages
- Backlog bypass: DB-recovered listings entered at `_evaluate()`, skipping ALL filters — vehicles, stale listings, and out-of-region listings reached notifications

### Solution: `listing_filter.py` + `FilterChain`
All filtering consolidated into `src/poob/scanner/listing_filter.py`:

**Architecture:** Predicate composition pattern. Each filter is a frozen dataclass with `__call__` → `FilterVerdict`. A `FilterChain` orchestrates execution, handles stage routing and tag-based exemptions.

**Filters (each defined once, applied by the chain):**
| Filter | Stage | What It Does |
|--------|-------|-------------|
| `SponsoredFilter` | PRE_ENRICHMENT | Rejects sponsored/boosted listings and "Ships to you" |
| `CategoryFilter` | PRE_ENRICHMENT + POST_ENRICHMENT | Uses `marketplace_listing_category_id` from GraphQL (100% reliable), falls back to keyword matching |
| `FreshnessFilter` | PRE_ENRICHMENT + POST_ENRICHMENT | Rejects listings older than `listing_max_age_hours` |
| `GeoDistanceFilter` | POST_ENRICHMENT | Haversine distance from user's configured center point, using lat/lng from detail page enrichment |
| `GarbageFilter` | POST_ENRICHMENT | Rejects UI artifacts ("See details", "Loading") and unenrichable titles with no description |

**Tag-based exemptions:** Watchlist-matched listings get a `watchlist_category_override` tag that exempts them from `CategoryFilter` (but NOT from geo/freshness/garbage filters). Tags are computed upstream, filters don't know about watchlists.

**Backlog fix:** Backlog listings now go through the FULL filter chain (both stages) before evaluation. Rejected backlog listings are marked `evaluated=True` so they don't reappear.

### Geo-Filtering: Haversine Distance
`src/poob/utils/geo.py` — pure `math` module implementation, no external dependencies.
- Center point resolved from `patrol_center_lat`/`patrol_center_lon` config, auto-resolved from `marketplace_default_location` city slug if unset
- Listings without coordinates pass through (benefit of the doubt — coordinates only available after detail enrichment)
- Null island (0, 0) detected and treated as "no coordinates"
- Accurate to ~0.3% at Wisconsin latitudes — more than enough for marketplace filtering

### GraphQL Field Extraction
`marketplace_listing_category_id` is now extracted from search responses (was always in the response but never parsed). Also extracts `delivery_types` and attempts to extract `location.latitude`/`location.longitude` from search results. A temporary debug log (`DEBUG_GQL_KEYS`) dumps all listing node keys for the first 3 listings per session — **remove after verifying field availability on next live run**.

### Brain Routing Fix
`src/poob/brain/poob.py` — when Groq (primary tool-calling LLM) is down, ALL messages route through the deal agent's own LLM cascade (Groq → NVIDIA → Gemini → Ollama) instead of fragile keyword matching. The deal agent handles tool-calling natively; casual messages pass through quickly.

### `_parse_evaluate_result` Shared Utility
Moved from `detail_extractor.py` to `src/poob/utils/content.py` as `parse_evaluate_result()`. Handles browser-use's `Page.evaluate()` returning JSON-stringified strings instead of Python objects. Used by `detail_extractor.py`, should be used by any file calling `page.evaluate()`.

### Dead Code
`src/poob/browser/detail_interceptor.py` — marked DEPRECATED. Facebook embeds listing data in data-sjs HTML tags, not XHR GraphQL calls. The CDP interceptor was built on this incorrect assumption. The actual extraction path is `detail_extractor.py` → `parse_data_sjs_payloads()`.

---

## One-Handler Discord Architecture (April 21 2026)

### Problem: Double-replies, cross-channel voice bleed, sibling-addressed responses

Logs showed every @mention producing TWO Poob messages in chat and a TTS playback even when the user wasn't in VC. Traced to **two `on_message` listeners** in parallel: `AgentMessageHandler.on_message` (always fires on @mention/DM) and the now-deleted `VoiceCog.on_message` (fired when guild had text-to-voice mode enabled). Secondary consequence: both listeners called `PoobBrain.respond(user_id=X)` concurrently, racing on the per-user history in `_histories`. VoiceCog passed the bare text without channel context; AgentHandler passed the full transcript. Both mutated history for the same user, and within a few turns the LLM was hallucinating names from polluted history (Noah's message → Poob addressing "Ben").

### Fix — Single owner per input modality

- **AgentMessageHandler owns text messages.** It's the only `on_message` listener for @mentions/DMs. Generates response via `PoobBrain.respond`, posts text, then asks siblings for side effects.
- **VoiceCog owns voice output.** Exposes `async def speak_if_in_channel(message, text) -> bool`. AgentHandler calls it after posting text; VoiceCog speaks iff `message.author.voice.channel == session.voice_client.channel`. No opt-in toggle — the in-VC check is deterministic.
- **MusicCog owns music state and the now-playing embed.** Exposes `build_now_playing_message(guild_id)`. AgentHandler calls it after a music-routing response to attach the embed + persistent View.

Deleted: `VoiceCog.on_message` (duplicate listener), `VoiceCog._text_to_voice` set, `!voice` toggle, `!say` command.

### Rule: text @mention → voice output is gated by VC co-membership

- User @mentions Poob in #general while in VC "Hawk Tuah Department" (same as Poob) → text reply in #general + TTS in VC.
- User @mentions Poob from DMs or from a channel while NOT in Poob's VC → text only, silent in voice.
- Consequence: Ben typing in chat while outside the VC no longer gets his message auto-spoken; Noah typing from outside the VC no longer blasts Poob's reply into the call.

### Rule: music request from text requires requester in Poob's VC

Music playback creates audio in VC — it only makes sense if the requester can hear it. `MusicCog.handle_music_request` now short-circuits with a polite refusal if the caller isn't in the bot's current VC (`play` action only — skip/pause/volume from buttons bypass this check because the button can only be clicked by someone in the channel anyway). When Poob isn't in any VC yet, `_auto_join_requester_vc(guild, user_id)` joins the **requester's** channel, not "most populated" — preserves the invariant.

---

## One-Handler Music Contract (April 21 2026)

**All music actions go through `MusicCog.handle_music_request(..., tool_args={...})`. There is no other entry point.**

Three input modalities produce the same `tool_args`:

| Source | How `tool_args` is built |
|---|---|
| Voice ("hey poob skip") | STT → `PoobBrain.respond_streaming` → LLM `music_assistant` tool call → handler |
| Text @mention (`@Poob skip`) | `PoobBrain.respond` → same LLM path → handler |
| Button click (`⏭` on now-playing embed) | `MusicControlsView` callback constructs `{"action": "skip"}` → handler |

The LLM's sole job is **classifying unstructured speech/text into structured `tool_args`**. Buttons bypass the LLM because their intent is already classified — identical contract, no divergent code paths.

**State sync:** `handle_music_request` refreshes `PoobBrain._music_playing_info` from live player state at the top of every invocation (`music_cog.py:180-188`). Button clicks therefore keep the brain's LLM-routing context accurate even though they skip the LLM itself. Player state is the single source of truth; brain reads, never caches.

**Consequence — all `!play`/`!skip`/`!queue`/etc. prefix commands are deleted.** Slash commands `/join` and `/leave` remain as the only command surface: they're VC-entry infrastructure (you can't @mention the bot in VC if it's not connected yet). All other interactions flow through @mention → LLM → tool_args, or button → tool_args.

### Now-playing UI

`src/poob/discord_bot/music_ui.py` implements:

- **`build_now_playing_embed(track, queue_size)`** — post-Rythm embed convention: hyperlinked title, top-right thumbnail from `info_dict['thumbnail']` (not hardcoded maxresdefault — 404s on ~15% of videos), duration, requester footer, single brand color (`#8B4FBE`).
- **`MusicControlsView(timeout=None)`** — persistent ActionRow with stable `custom_id`s (`poob:music:skip`, `poob:music:pause`, etc.). Re-registered via `bot.add_view(...)` in `on_ready` so buttons survive restarts. Callbacks build `tool_args` and call the handler.

Deliberate non-goals: **no live-updating progress bar** (Discord's 5-edit/5-second per-channel rate limit makes it counterproductive), **no Components V2 containers** (overkill for a single-song card).

---

## Wake-Word Dual-Gate (April 21 2026)

### Problem: bot re-triggered by its own music via mic loopback

Ben said "Hey Poob play jah jah jah blah blah blah" → music played (Armin van Buuren "BLAH BLAH BLAH"). 12 seconds later, while the song was playing, a second "Text wake word match" fired with near-identical transcript `'Hey, Poob. Play jah jah jah blah blah blah.'` attributed to Ben, producing a duplicate queue and a second Toob reaction. Ben hadn't said anything.

Root cause: Ben's speakers played the song → his mic captured the loopback → Discord transmitted as his audio → Deepgram transcribed the lyrics + ambient noise + any murmurs. With keyterm bias (`keyterm=Poob&keyterm=play&keyterm=skip...`), Deepgram hallucinated a wake phrase from the song's "blah blah blah" lyrics. The old rule accepted **text match alone** as addressing → hallucination became a command.

### Fix — require BOTH acoustic AND semantic confirmation

`src/poob/voice/dual_pipeline.py:_emit_utterance`:

```python
is_addressed = text_match and pipeline.is_active
```

`pipeline.is_active` is True iff openwakeword fired during the utterance or the 1.5 s pre-speech window. Requires an actual wake-word acoustic signal, not a bias-primed transcript hallucination. Matches the industry standard (Alexa / Google Assistant / Siri all require acoustic wake-word detection as the primary gate).

Cost: very rare real requests where openwakeword totally misses will now fall through to passive. Benefit: self-triggering and ambient-hallucination classes of bug are eliminated. Acceptable tradeoff.

### Common pitfall (saved for future)

Any signal loop where the bot produces audio that can be captured by a mic in the same call creates this class of bug. Keyterm biasing in the ASR amplifies it — primed words hallucinate from weak signals. If we ever expose other bias-prone keyterms (deal names, brands) the same symmetric dual-gate rule applies: the semantic layer is confirmation, not primary.

---

## Toob Voice Tuning (April 21 2026)

`src/poob/voice/session.py:_synthesize_toob` — FFmpeg filter chain updated from `atempo=1.7` to `atempo=1.85` (faster delivery per user feedback) and a subtle `vibrato=f=5.5:d=0.15` added between the atempo and bass stages. 5.5 Hz is human-prosody territory (natural vibrato is 4-7 Hz); depth 0.15 is shallow enough to preserve the warlord menace without sounding drunk. Final `volume=1.35` (+2.6 dB) sits Toob prominent in the mix.

`src/poob/brain/poob.py:_wrap_music_response` — system prompt tightened from "One sentence. Under 15 words" to "ONE short sentence. 8-12 words MAX" plus a hard `toob_max_tokens = min(max_tokens, 60)` cap. Prevents drift past the word limit at high temperature.

TTS output volume (non-Toob and Toob alike) bumped from `PCMVolumeTransformer(volume=2.0)` to `2.5` in both the music-overlay and standalone playback paths — Poob was sitting quieter than the music bed after ducking.
