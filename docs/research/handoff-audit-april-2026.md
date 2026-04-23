---
type: research
status: superseded
date: 2026-04-01
tags: [audit, handoff, scanner, historical]
superseded_by: [[unified-filter-pipeline]]
related: [[unified-filter-pipeline]] [[common-scanner-pitfalls]] [[just-listed-rework]]
---

# Handoff: Complete Audit of Issues Identified (March-April 2026 Session)

**Context:** This document catalogs every issue identified during an extended debugging and development session. Many "fixes" were applied as bandaids that violate the project's core principles: robust, modular, integrated, industry-standard code with no hardcoded workarounds. The new developer must READ the full codebase (`docs/technical_notes.md`, `docs/research/*.md`, `.claude/CLAUDE.md`) before making ANY changes.

**Critical principle:** Users configure their own search radius, location, and preferences. The system must respect those parameters architecturally — NOT with hardcoded allowed-state lists, NOT with hardcoded category patterns that grow endlessly, NOT with "safety net" checks piled on top of each other. If a listing from California reaches the notification stage, the fix is WHERE it entered the pipeline, not adding a state whitelist at the end.

---

## ISSUE 1: Facebook Returns Out-of-Region Listings Despite Radius Parameter

**What happens:** Anonymous GraphQL searches include `radius_km=64` (~40 miles from Appleton, WI), but Facebook returns listings from California, Texas, etc. — thousands of miles away. This affects broad category keyword searches ("electronics", "free stuff", "tools") more than specific keyword searches.

**What was done (WRONG):** A hardcoded `_ALLOWED_NOTIFY_STATES` set was added with specific US state abbreviations (WI, MN, IA, IL, MI, IN). This is a bandaid that:
- Breaks if the user moves or changes their search area
- Doesn't use the user's actual configured radius
- Would need manual editing for every new user
- Was copy-pasted into multiple places (post-enrichment filter AND notification safety net)

**What SHOULD be investigated:** 
- The GraphQL response includes `location.latitude` and `location.longitude` for each listing. A proper fix would compute the haversine distance from the user's configured center point and reject listings beyond `radius * 1.5` (with margin for Facebook's approximation)
- The anonymous GraphQL `_build_variables()` already sends lat/lng/radius — investigate why Facebook ignores it for category searches specifically
- Check if `deliveryMethod=local_pick_up` parameter helps constrain results geographically

**Files affected:** `src/poob/scanner/patrol_engine.py` (lines ~97-103 for the hardcoded states, lines ~400-430 for the post-enrichment filter, lines ~1700-1715 for the notification safety net)

---

## ISSUE 2: Vehicle/Motorcycle Listings Reaching Notifications

**What happens:** Cars, trucks, motorcycles, RVs appear in deal notifications despite `_EXCLUDED_CATEGORY_PATTERNS` containing 65+ vehicle-related keywords.

**Root causes identified:**
1. **Watchlist exemption bypass:** Any listing tagged as a watchlist match (even accidentally) bypasses the category filter entirely. A Yamaha motorcycle matched to a watchlist item skips the "yamaha" motorcycle pattern check.
2. **Category filter runs before enrichment:** At filter time, descriptions are empty (Facebook doesn't return them in search results). Vehicles identifiable only from their description pass through.
3. **Incomplete patterns:** "Winnebago", "RV", "motorhome", "pit bike", "hatchback", "sedan 4D" etc. were missing and had to be added one by one as they appeared in logs.

**What was done (WRONG):** 
- Kept adding more and more hardcoded strings to the pattern set
- Added a "post-enrichment category re-check" (running the same filter twice)
- Added ANOTHER check in `_notify()` as a "safety net" — three layers of the same check
- The pattern-based approach is fundamentally fragile: new vehicle makes, models, and terms will always slip through

**What SHOULD be investigated:**
- Facebook's GraphQL responses include `marketplace_listing_category_id`. This is a NUMERIC category ID that Facebook assigns. Filtering by category ID is 100% reliable — no pattern matching needed.
- The VLM already sees the listing images. It could be asked to classify the item category as part of its evaluation, and vehicle categories could be rejected post-VLM
- The watchlist exemption from category exclusion needs rethinking: why should a listing tagged `_watch_item_id=espresso` bypass the vehicle filter? The exemption was meant for users who explicitly watch excluded categories (e.g., someone who wants couch deals via DM), but it's too broad

**Files affected:** `src/poob/scanner/patrol_engine.py` (lines ~55-97 for patterns, ~302-304 for exemption, ~1567-1615 for the filter function, ~1689-1699 for the notification safety net)

---

## ISSUE 3: Detail Page Enrichment — The `Page.evaluate()` String Bug

**What happened:** For WEEKS, detail page enrichment returned `enriched=0` on every single run. The root cause: browser-use's `Page.evaluate()` returns JSON-stringified strings (`'[]'`, `'{"key": "value"}'`), not Python objects. Every `page.evaluate()` call was receiving strings, and the code treated them as truthy/falsy without parsing.

**What was done (CORRECT):** A `_parse_evaluate_result()` helper was added that `json.loads()` the return value. Applied to all `page.evaluate()` calls in `detail_extractor.py`. This fixed enrichment: `enriched=0` → `enriched=43/48`.

**What still needs attention:**
- This same bug may exist in OTHER files that call `page.evaluate()` — the patrol scanner, direct scanner, and JS extractor all use `page.evaluate()` and may be silently getting string results
- `_parse_evaluate_result()` is defined locally in `detail_extractor.py` — it should be a shared utility if other files need it

**Files affected:** `src/poob/sites/facebook/detail_extractor.py`, potentially `src/poob/sites/facebook/js_extractor.py`, `src/poob/sites/facebook/patrol_scanner.py`

---

## ISSUE 4: Facebook Embeds Detail Data in HTML, NOT Separate XHR

**What happened:** A CDP network interceptor (`DetailGraphQLInterceptor`) was built to capture `/api/graphql/` responses during detail page navigation. After extensive debugging (session_id scoping, Playwright vs CDP vs browser-use API differences), it was discovered that Facebook embeds listing detail data in the initial HTML via `<script type="application/json" data-sjs>` tags — NOT as separate XHR GraphQL calls. The CDP interceptor captures only platform queries (messenger, backup, screen_time_sync, etc.).

**What was done:** The CDP interceptor file (`browser/detail_interceptor.py`) still exists but is not in the active extraction path. The data-sjs extraction (Tier 1) is what actually works.

**What needs cleanup:** The `detail_interceptor.py` file should either be removed or clearly marked as unused. Its CDP event handlers may still be registered and firing (generating debug-level log noise). The detail_extractor.py docstring was updated but there may be stale references elsewhere.

**Files affected:** `src/poob/browser/detail_interceptor.py` (potentially unused), `src/poob/sites/facebook/detail_extractor.py`

---

## ISSUE 5: Anonymous GraphQL Browse Returns ~1 Listing

**What happens:** The anonymous GraphQL browse (empty query, `__user=0`) returns only 1 listing per cycle. Facebook's anonymous endpoint doesn't serve a meaningful feed without search context.

**What was done (PARTIALLY CORRECT):** Category-based keyword searches were added ("electronics", "furniture", "appliances", "free stuff", "sporting goods", "toys", "tools"). This increased collection from 1 to ~250 listings per cycle. A separate anonymous headless browser was also added for DOM-based empty-query sweeping (adds ~18 listings).

**What needs attention:**
- The category list is hardcoded in `config.py` as `patrol_anonymous_browse_categories`. This is reasonable as config but should be documented
- The anonymous browser's DOM sweep navigates with the authenticated browser's search patterns (scroll_until_stable) — it should use its own simpler pattern since it has no login
- Rate limiting: 8 categories * 2 pages * 6s delay = ~96s. Combined with watchlist searches, total collection phase is ~3-5 minutes. Acceptable but worth monitoring.

**Files affected:** `src/poob/scanner/patrol_engine.py` (`_fetch_anonymous_graphql`), `src/poob/main.py` (anonymous browser creation), `src/poob/config.py`

---

## ISSUE 6: Descriptions Never Reach Triage or VLM

**What happens:** Facebook's GraphQL search API does NOT return `redacted_description` — only detail page queries do. Before the `_parse_evaluate_result` fix, enrichment returned 0 descriptions. After the fix, ~80-90% of enriched listings get descriptions. However:
- Backlog listings (from DB) have no descriptions if they were saved before enrichment worked
- The pre-enrichment cap (50 listings) means listings 51+ never get descriptions
- Triage and VLM still evaluate some listings with empty descriptions

**What was done:** VLM prompt includes `[No description available]` when description is empty, directing it to rely on photos. Stage 3 logging reports `with_description=N`.

**What needs attention:** The fundamental gap is that descriptions only come from detail page visits. If enrichment is skipped or fails, the VLM evaluates blind. This is an architectural reality of Facebook's API, not a bug.

**Files affected:** `src/poob/sites/facebook/detail_extractor.py`, `src/poob/skills/vlm_evaluator.py`

---

## ISSUE 7: Post-Enrichment Freshness — Facebook Serves Ancient Listings

**What happens:** Despite `daysSinceListed=1`, Facebook returns listings from weeks or months ago. One listing was from January 2025 (10,267 hours old). The pre-enrichment freshness filter can't catch these because most listings lack `creation_time` from GQL search. After enrichment extracts `creation_time` from data-sjs, stale listings are dropped.

**What was done (CORRECT):** Post-enrichment freshness re-check (Step 2i) drops listings older than `listing_max_age_hours` (6 hours) using the timestamp extracted during enrichment. This catches 30-60% of enriched listings as stale per cycle.

**What needs attention:** 
- 30-60% stale means 30-60% of enrichment time is wasted on listings that will be dropped
- A smarter approach: if GQL returns `creation_time` (it sometimes does), filter BEFORE enrichment
- The enrichment cap (50) should ideally apply AFTER freshness filtering, not before

**Files affected:** `src/poob/scanner/patrol_engine.py` (post-enrichment freshness filter, ~lines 360-390)

---

## ISSUE 8: Backlog Listings Bypass All Post-Collection Filters

**What happens:** Backlog listings (recovered from DB via `get_unevaluated()`) enter the pipeline at `_evaluate()`, AFTER all filtering steps. They bypass:
- Category exclusion (vehicles get through)
- Freshness filter (stale listings get evaluated)
- Location filter (California listings get evaluated)
- Garbage title filter (empty titles get VLM calls)
- Enrichment (no descriptions)

**What was done (PARTIAL):** 
- Backlog garbage filter catches empty titles and FB UI artifacts, marks them evaluated permanently
- Backlog listings are now included in `listing_map` for `_notify()` (fixes silent notification failures)
- Category and location "safety nets" were added to `_notify()` — but these are bandaids

**What SHOULD be investigated:** Backlog listings should go through the same filter pipeline as fresh listings. Either:
- Run the filter chain on backlog listings before merging them into `to_evaluate`
- Or create a `_filter_listing()` function that applies ALL checks and use it everywhere

---

## ISSUE 9: Cross-Contamination During Detail Page Enrichment

**What happens:** When enrichment visits a listing's detail page, Facebook sometimes redirects to a different listing (the original was sold/deleted/claimed). The data-sjs extraction then pulls the WRONG listing's title, description, and images. This caused:
- "Free area rug" showing a Samsung refrigerator photo and description
- Wrong images in notifications (chairs photo for a Keurig listing)

**What was done (PARTIALLY CORRECT):**
- Title overlap validation: if enriched title has <30% word overlap with original title (and original has 3+ words), enrichment is discarded
- Image merging changed from prepend to append (original search thumbnail stays as `[0]`)
- Image caps: 8 max from data-sjs regex, 5 max appended during enrichment

**What needs attention:**
- The 30% threshold and 3-word minimum are arbitrary — may need tuning
- The data-sjs regex grabs ALL `"uri": "https://...scontent..."` URLs from the payload, including images from the search feed behind the detail overlay. A more targeted extraction (only within `listing_photos` context) would be more reliable

**Files affected:** `src/poob/sites/facebook/detail_extractor.py`, `src/poob/sites/facebook/detail_graphql_extractor.py`

---

## ISSUE 10: Watchlist Contamination in Public Channel

**What happens:** 44% of public channel deals are coffee/espresso or glassware — categories from the user's watchlist. This isn't personalization contamination (the anonymous GQL is truly depersonalized). It's that "appliances" category genuinely returns coffee makers, and "free stuff" returns kitchen items.

**What was done:** Category searches were added to diversify. But the fundamental issue remains: watchlist-related items are common in broad categories.

**What SHOULD be investigated:** Whether public channel deals should exclude items that match ANY active watchlist interest (since those would be DM'd to the user anyway). This is a product decision, not a code bug.

---

## ISSUE 11: "Brain's Offline" When Asking About Wishlist

**What happens:** When Groq (primary tool-calling LLM) fails/times out, the fallback path (Cerebras → Ollama) has NO tool calling capability. "What's my wishlist?" requires calling `show_wishlist` tool, which only works via Groq's function calling.

**What was done:** Keyword-based direct tool routing was added — if the message contains "wishlist", "scan", "add", "remove", etc., it routes directly to the deal agent (which has its own LLM cascade with tool calling).

**What needs attention:** The keyword list is hardcoded and fragile. A more robust approach would be to have the deal agent handle ALL messages when Groq is down, since it has its own tool-calling LLM cascade.

**Files affected:** `src/poob/brain/poob.py` (~lines 195-215)

---

## ISSUE 12: VLM Provider Exhaustion and Rate Limiting

**What happens:** Google models (Gemini Flash, Flash Lite, Gemma) hit RESOURCE_EXHAUSTED after ~8-15 evaluations per cycle due to undocumented IPM (Images Per Minute) limits. Groq Vision hits 429 after ~20 evaluations. The cascade falls through to Mistral Pixtral and OpenRouter, but late-cycle evaluations often have only 1-2 voters.

**What needs attention:**
- Together.ai returns 402 (credit limit exceeded) — should be removed or requires paid credits
- OpenRouter Mistral returns 404 (model endpoint removed) — needs model update
- Google's IPM limit is undocumented and varies — a leaky bucket rate limiter would be more reliable than the current simple cooldown
- The `docs/research/free-vlm-providers.md` has a comprehensive ranked list of free providers

---

## ISSUE 13: Listings Where `listing.price` is Set From the DB

**What was done correctly:** The `listing_repo.py` ON CONFLICT clause was updated by the user to use COALESCE for `description`, `seller_name`, and `posted_at` — so enrichment fills gaps but never overwrites existing good data. This is the correct pattern.

---

## Summary of Bandaids That Need Architectural Fixes

| Bandaid | Where | What Should Replace It |
|---------|-------|----------------------|
| `_ALLOWED_NOTIFY_STATES` hardcoded set | patrol_engine.py | Haversine distance check using user's configured radius + lat/lng |
| `_EXCLUDED_CATEGORY_PATTERNS` growing keyword list | patrol_engine.py | Facebook's `marketplace_listing_category_id` numeric filtering |
| Triple category check (pre-enrichment + post-enrichment + notify) | patrol_engine.py | Single filter function called at the right point in the pipeline |
| `_UNINFORMATIVE_TITLES` hardcoded set | patrol_engine.py | Already handled by garbage filter — redundant |
| `_TOOL_INTENTS` keyword matching for brain routing | poob.py | Route ALL messages through deal agent when Groq fails |
| Backlog bypasses all filters | patrol_engine.py | Run filter chain on backlog before evaluation |
