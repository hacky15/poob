---
type: plan
status: superseded
date: 2026-03-17
tags: [scanner, vlm, pipeline, historical]
superseded_by: [[unified-filter-pipeline]]
related: [[unified-filter-pipeline]] [[vlm-triage-pipeline]] [[common-scanner-pitfalls]]
---

# Implementation Plan — Pipeline Robustness Overhaul

Based on a full codebase audit + 5 research documents + run analysis. Most items landed in the [[unified-filter-pipeline|unified filter pipeline refactor]]. Retained for historical context.

## Guiding Principles
1. Every change must be a **net positive** — no new debt
2. Fix at the **right architectural layer** — not downstream workarounds
3. **Modular** — each fix is independent and testable
4. **Research-backed** — every decision references `docs/research/` findings
5. **Measured** — every fix has a "how to verify" step

---

## Priority 1: CRITICAL — Fix Data That Never Reaches the Pipeline

These bugs mean listings are evaluated with missing/wrong data. Everything downstream suffers.

### 1A. Fix `listing_repo.py` ON CONFLICT Clause (5 min, zero risk)

**Problem:** Enriched `description` and `posted_at` are silently dropped on upsert.
**File:** `src/poob/storage/repositories/listing_repo.py` lines 36-45
**Fix:** Add missing fields to UPDATE SET:
```sql
ON CONFLICT(site, external_id) DO UPDATE SET
    title = excluded.title,
    description = COALESCE(excluded.description, listings.description),
    price = excluded.price,
    location = excluded.location,
    seller_name = excluded.seller_name,
    image_urls = excluded.image_urls,
    listing_url = excluded.listing_url,
    posted_at = COALESCE(excluded.posted_at, listings.posted_at),
    scraped_at = excluded.scraped_at,
    raw_data = excluded.raw_data,
    is_sponsored = excluded.is_sponsored
```
Use COALESCE so enrichment fills gaps but never overwrites existing good data.
**Verify:** After a run, query `SELECT COUNT(*) FROM listings WHERE description IS NOT NULL AND length(description) > 10 AND scraped_at > datetime('now', '-1 hour')`.

### 1B. Fix GraphQL Price Cents Conversion (10 min, zero risk)

**Problem:** `amount_with_offset_amount` is in CENTS but parsed as dollars (100x inflation).
**File:** `src/poob/browser/graphql_interceptor.py` lines ~207-216
**Fix:**
```python
# Try dollar amount first (most common)
amount_str = price_obj.get("amount")
if not amount_str:
    # Fallback: cents fields — MUST divide by 100
    cents_str = (
        price_obj.get("amount_with_offset_in_currency")
        or price_obj.get("amount_with_offset_amount")
        or price_obj.get("amount_with_offset")
    )
    if cents_str:
        try:
            amount_str = str(float(str(cents_str)) / 100.0)
        except (ValueError, TypeError):
            pass
if not amount_str:
    # Last resort: formatted display string
    amount_str = str(price_obj.get("formatted_amount") or price_obj.get("text", "") or "")
```
**Verify:** Add unit test with `{"amount_with_offset_in_currency": "1850"}` → expect `price=18.50`.
**Reference:** `docs/research/fb-graphql-schema.md` — Price Field Formats table.

### 1C. Fix VLM Voting Early-Exit False Demotions (10 min, zero risk)

**Problem:** When 2/3 voters agree and the 3rd task is cancelled, the cancellation increments consecutive failure counter → healthy providers get demoted for 10 minutes.
**File:** `src/poob/llm/vlm_cascade.py` — voting phase, task cancellation handler
**Fix:** When cancelling tasks due to early exit, do NOT increment failure counter. Add a flag or check the cancellation reason.
**Verify:** Check logs for `vlm.voting_partial_failures` — cancelled providers should not appear in `_demoted_until` shortly after.

---

## Priority 2: HIGH — Replace Broken Detail Enrichment

### 2A. Implement GraphQL Network Interception for Detail Pages

**Problem:** OG/JSON-LD extraction is completely broken (returns empty for ALL listings). This is the ONLY way to get descriptions, full image sets, creation timestamps, and condition data from detail pages.

**Research:** `docs/research/fb-detail-page-extraction.md` — confirmed by multiple sources.

**Implementation — Three-tier extraction in `detail_extractor.py`:**

1. **Tier 1: CDP Network Interception** (primary)
   - Before navigating to detail URL, register a CDP `Network.responseReceived` listener
   - Filter for responses where URL contains `/api/graphql/`
   - In the response body, search for JSON containing `marketplace_listing_title` or `__typename: "MarketplaceListing"`
   - Parse out: title, `redacted_description.text`, `listing_price`, `creation_time`, `condition`, `listing_photos[].image.uri`, `marketplace_listing_seller`, `location_text`, GPS coords
   - Wait up to 10s for the GraphQL response to arrive after navigation

2. **Tier 2: Embedded Relay JSON** (fallback)
   - After page loads, query `document.querySelectorAll('script[type="application/json"][data-sjs]')`
   - Search each script's content for `marketplace_listing_title`
   - Regex extract: `"marketplace_listing_title":"(.*?)"`, `"formatted_amount":"(.*?)"`, `"creation_time":(\d+)`, `"text":"(.*?)"` (description)

3. **Tier 3: DOM Structural Selectors** (emergency)
   - `h1 span[dir="auto"]` → title
   - Price: look for `span` near the title with `$` or currency formatting
   - `img[src*="scontent"]` within `[role="main"]` → images
   - Freshness badge text → `_parse_freshness()` for timestamp

**Architecture:**
- New class `GraphQLDetailInterceptor` alongside existing `detail_extractor.py`
- Returns a `DetailResult` dataclass with all extracted fields
- `detail_extractor.py` calls this first, falls back to existing DOM extraction

**Verify:** After a run, check `SELECT title, description, price, posted_at FROM listings WHERE scraped_at > datetime('now', '-1 hour') AND description IS NOT NULL` — expect 80%+ to have descriptions.

### 2B. Add `posted_at` to Triage Context (5 min)

**Problem:** Triage LLM doesn't know how fresh a listing is — can't weight urgency by recency.
**File:** `src/poob/skills/text_triage.py` — `_build_listings_block()`
**Fix:** Add `Posted: {posted_at or "Unknown"}` to the listing block between Condition and Location.
**Impact:** Triage can now prefer "Listed 1 hour ago" over "Listed yesterday". Low risk — additive context only.

---

## Priority 3: MEDIUM — VLM Quota Resilience

### 3A. Add Images-Per-Minute (IPM) Tracking

**Problem:** Google AI Studio has undocumented IPM limit (~2-10/min free tier). Bursting evaluations exhausts it before daily RPD.
**File:** `src/poob/llm/vlm_cascade.py`
**Implementation:**
- Add `_ipm_window: deque[float]` (timestamps of recent image submissions) per provider
- Before each VLM call, check if images sent in last 60s exceeds provider's IPM limit
- If over limit, skip provider (same as quota exhaustion), cascade to next
- IPM limits (configurable):
  - Google models: 8 IPM (conservative, below undocumented 10)
  - Groq Vision: 25 IPM
  - Others: 30 IPM (or no limit for local)

### 3B. Add New Free VLM Providers

**Research:** `docs/research/free-vlm-providers.md` — ranked by quality with exact limits.

**Add these providers to the VLM cascade:**

| Priority | Provider | Model | Limit | Integration |
|----------|----------|-------|-------|-------------|
| After Groq | Together.ai | Llama-Vision-Free | Dynamic/unlimited | OpenAI-compatible endpoint |
| After Together | Mistral | Pixtral 12B | 1 RPS / unstated daily | OpenAI-compatible |
| After Mistral | Cloudflare Workers AI | Llama 4 Scout 17B | 10K neurons/day | REST API |

Each provider: ~30 lines of code (OpenAI-compatible client with provider-specific base_url).

**New cascade order:**
```
gemini_flash → gemini_flash_lite → gemma_vlm → groq_vision →
together_vision → mistral_pixtral → openrouter_mistral →
gemini_pro (tiebreaker) → cloudflare_vision → openrouter_nemotron → ollama
```

**Verify:** Run a cycle with many listings. Check `vlm_cascade.built` log — should show 11+ providers. Check that `All VLM providers exhausted` does NOT appear.

### 3C. Per-Cycle Budget Allocation

**Problem:** Morning patrol cycles burn all daily VLM quota, leaving evening cycles with nothing.
**Implementation:**
```python
budget_per_cycle = provider_rpd * 0.80 / expected_daily_cycles
# expected_daily_cycles calculated from patrol intervals
# When cycle usage hits budget, demote provider for remaining cycle duration
```
- 20% reserve for retries
- Prevents early cycles from starving later ones
- Config: `vlm_budget_reserve_pct = 0.20`

---

## Priority 4: MEDIUM — Search Provider Expansion

### 4A. Add Mojeek Search (2,000/day FREE)

**Research:** `docs/research/free-search-providers.md`
**Implementation:**
- REST API: `https://api.mojeek.com/search?q=QUERY&api_key=KEY&fmt=json`
- Parse `results[].title`, `results[].desc`, `results[].url`
- Insert in cascade after Google CSE, before SearXNG

### 4B. Add Google Custom Search Engine (100/day FREE)

- REST API: `https://www.googleapis.com/customsearch/v1?q=QUERY&key=KEY&cx=ENGINE_ID`
- Most reliable results
- Insert as first in cascade (highest quality, lowest quota)

### 4C. Updated Search Cascade
```
Google CSE (100/day, best quality) →
Mojeek (2000/day, good quality) →
Tavily (1000/month, when available) →
Serper (one-time credits, when available) →
SearXNG (unlimited, self-hosted) →
empty result
```

**Verify:** After a run, check logs for `SearXNG search complete` — if Mojeek/CSE are catching most queries, SearXNG calls should drop.

---

## Priority 5: LOW — Polish & Observability

### 5A. SearXNG Engine Optimization

**Research:** `docs/research/free-search-providers.md` — SearXNG config section
- Disable academic engines (arXiv, PubMed, Wikipedia)
- Enable shopping engines (eBay native, Google Shopping)
- Disable internal rate limiter (private instance)
- Enable Valkey/Redis caching

### 5B. Add Pipeline Timing Logs

Add structured timing to each phase:
```
patrol.phase_timing  collect=210s  filter=0.5s  enrich=150s  evaluate=163s  notify=16s  total=540s
```
Makes it easy to identify regression without parsing timestamps manually.

### 5C. Watchlist Item Sanity

The "cookies" watchlist item is matching baked goods sellers. Options:
1. Add notes: "only cookie jars, cookie cutters, vintage — not actual food/baked goods"
2. Add negative keywords: "not fresh, not homemade, not bakery, not cupcakes"
3. Both (recommended)

---

## Implementation Order

```
Week 1 (Quick Wins — immediate impact):
  Day 1: 1A (DB upsert fix) + 1B (cents conversion) + 1C (demotion fix) + 2B (posted_at in triage)
  Day 2: 3B (add Together.ai + Mistral VLM providers)
  Day 3: 3A (IPM tracking) + 4A/4B (Mojeek + Google CSE search)

Week 2 (Detail Enrichment Overhaul):
  Day 1-2: 2A (GraphQL detail page interception — Tier 1 CDP)
  Day 3: 2A Tier 2 (data-sjs regex fallback)
  Day 4: 2A Tier 3 (DOM selectors) + integration testing

Week 3 (Polish):
  3C (per-cycle budget) + 5A (SearXNG optimization) + 5B (timing logs) + 5C (watchlist fixes)
```

---

## What NOT To Change

Based on audit, these are working correctly — leave them alone:

1. **Watchlist triage bypass** — working as designed, prevents 77% false kills
2. **Savings enforcement thresholds** — GOOD/GREAT/INCREDIBLE dollar amounts are correct
3. **Price display consistency** — three-way logic (known/free/unknown) is correct everywhere
4. **Freshness filter** — keeping no-timestamp listings is correct (dedup prevents re-evaluation)
5. **Dedup by external_id only** — sufficient for Facebook's stable listing IDs
6. **Search cascade fallthrough** — Tavily → Serper → SearXNG working correctly
7. **VLM prompt context** — already comprehensive (15+ data points)
8. **DOM scroll-until-stable** — working correctly with int() coercion fix
9. **GraphQL concurrent pagination** — 2-semaphore at 6s delay is optimal
10. **Anonymous GraphQL session bootstrap** — datr + lsd extraction working

---

## Verification Checklist (Post-Implementation)

Run a full patrol cycle and verify:

- [ ] `description IS NOT NULL` for 80%+ of enriched listings
- [ ] `price IS NOT NULL` for 80%+ of GraphQL-sourced listings
- [ ] `posted_at IS NOT NULL` for 70%+ of all listings
- [ ] No `discount_pct=100.0` for listings where `price=None`
- [ ] No `All VLM providers exhausted` in logs
- [ ] `vlm_cascade.built` shows 10+ providers
- [ ] `Retail lookup provided MSRP (listing price unknown)` count < 30% of evaluated listings
- [ ] Total patrol cycle < 10 minutes
- [ ] Deal count per cycle is 5-15 (not 0 and not 29)
- [ ] No false demotions visible in logs (cancelled tasks don't trigger demotion)

---

## Files to Create/Modify

### New Files:
- `src/poob/browser/detail_interceptor.py` — GraphQL intercept for detail pages
- Provider adapters for Together.ai, Mistral, Cloudflare (in `llm/vlm_cascade.py` or separate files)
- `src/poob/search/google_cse.py` — Google Custom Search
- `src/poob/search/mojeek.py` — Mojeek search

### Modified Files:
- `src/poob/storage/repositories/listing_repo.py` — Fix ON CONFLICT (1A)
- `src/poob/browser/graphql_interceptor.py` — Fix cents conversion (1B)
- `src/poob/llm/vlm_cascade.py` — Fix demotions (1C), add IPM (3A), add providers (3B), budget (3C)
- `src/poob/sites/facebook/detail_extractor.py` — Replace OG/JSON-LD with interceptor (2A)
- `src/poob/skills/text_triage.py` — Add posted_at to context (2B)
- `src/poob/skills/web_search.py` — Add new search providers (4A/4B)
- `src/poob/config.py` — New API keys and provider configs
