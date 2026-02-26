# Phase 5: Smart Deal Radar - Skill-Based Price Intelligence

## Goal
Replace the naive LLM-guessing Deal Radar with a skill-based system where the LLM
orchestrates tool calls to gather real market data before scoring deals. The LLM decides
what it knows, what it doesn't, and which skills to invoke.

## Design Principles

1. **LLM as orchestrator** - The LLM is the decision-maker. It evaluates each listing,
   determines what information is missing, and invokes skills to fill the gaps.
2. **Skills as tools** - Each pricing capability is a discrete, testable skill the LLM
   can call via LangChain tool-use (structured output).
3. **Data-backed scores only** - No deal is flagged without at least one external price
   reference (eBay sold data, retail price lookup, or category estimate). Pure LLM
   guesses are rejected.
4. **Vision as fallback** - Visual identification is only used when title/description
   text is too vague for confident item identification.
5. **Balanced confidence** - The LLM can flag deals with category-level pricing (e.g.
   "mid-range dining table") when it can't determine the exact model. This catches
   more deals than conservative mode while filtering obvious false positives.

## Architecture

### Skill Definitions

Each skill is a LangChain `Tool` the LLM can invoke. They live in
`src/agentic_scraper/skills/`.

#### 1. `identify_item` (skills/identify.py)
**Purpose:** Extract structured item details from listing text.
**Input:** title (str), description (str)
**Output:** ItemIdentification dataclass:
  - item_name: str (best guess at specific product name)
  - brand: str | None
  - model: str | None
  - category: str (e.g. "electronics/gaming/console", "furniture/table")
  - condition: str | None ("new", "like new", "good", "fair", "parts")
  - confidence: float (0.0-1.0)
  - needs_visual: bool (True if text is too vague)

**Implementation:** LLM call with structured output schema. No external API needed -
this is pure text analysis the LLM does well.

#### 2. `visual_identify` (skills/visual.py)
**Purpose:** Identify an item from listing photos when text is insufficient.
**Input:** image_urls (list[str]) or screenshot_b64 (str)
**Output:** Same ItemIdentification dataclass as identify_item.
**When invoked:** Only when identify_item returns confidence < 0.5 or needs_visual=True.

**Implementation:** Fetch image via httpx, send to vision-capable LLM (Qwen2.5-VL,
LLaVA, etc.). Falls back to identify_item result if vision model unavailable.

**Config:** Requires a vision-capable model. Config field: `vision_model` in AppConfig.
Can be the same as the main model if it supports vision, or a separate model.

#### 3. `lookup_ebay_sold` (skills/ebay_lookup.py)
**Purpose:** Search eBay completed/sold listings to get real market price data.
**Input:** query (str), condition (str | None)
**Output:** PriceLookupResult dataclass:
  - median_price: float
  - average_price: float
  - min_price: float
  - max_price: float
  - sample_count: int (number of sold listings found)
  - source: str ("ebay_sold")
  - search_query: str
  - confidence: float (higher with more samples)

**Implementation (Hybrid approach):**
1. **Try direct HTTP first** (~2s): Use httpx to fetch eBay search results page with
   `LH_Complete=1&LH_Sold=1` params. Parse prices from HTML with a simple regex/parser.
   No login required for eBay sold items.
2. **Fall back to browser-use** (~30s): If HTTP scraping fails (eBay changes HTML,
   CAPTCHA, etc.), create a browser-use agent to navigate eBay, filter to "Sold Items",
   and extract prices.
3. **LLM filters results:** After getting raw prices, the LLM reviews the titles to
   filter out irrelevant results (accessories, broken items, different variants) before
   calculating statistics.

**eBay search URL pattern:**
```
https://www.ebay.com/sch/i.html?_nkw={query}&LH_Complete=1&LH_Sold=1&_sop=13
```
(`_sop=13` sorts by most recent, `LH_Complete=1&LH_Sold=1` filters to sold items)

#### 4. `lookup_retail_price` (skills/retail_lookup.py)
**Purpose:** Find the original MSRP or current new retail price for an item.
**Input:** item_name (str), brand (str | None), model (str | None)
**Output:** PriceLookupResult dataclass (same as ebay, source="retail")

**Implementation:** Quick web search via httpx. Search query: "{brand} {model} price" or
"{item_name} retail price MSRP". Parse the first few results for price mentions. This
gives a ceiling reference - the used price should be well below retail.

#### 5. `estimate_by_category` (skills/category_estimate.py)
**Purpose:** Provide a price range when exact item identification fails.
**Input:** category (str), condition (str | None), description_hints (str)
**Output:** CategoryEstimate dataclass:
  - low_price: float
  - high_price: float
  - typical_price: float
  - source: str ("category_estimate")
  - confidence: float (always lower than data-backed lookups)

**Implementation:** LLM call with a prompt like "What is the typical price range for a
used {category} item in {condition} condition?" Combined with depreciation heuristics:
- Electronics: 15-25% depreciation year 1, then 10-15%/year
- Furniture: 30-50% of retail when used
- Clothing: 20-40% of retail
- Vehicles: KBB-style depreciation curves

This is the lowest-confidence skill - used when eBay lookup returns no results and
retail price is unknown. Still better than a pure LLM guess because it's category-anchored.

### Orchestration Flow (skills/orchestrator.py)

The SmartDealRadar replaces the old DealRadar. It uses LangChain's tool-use pattern:

```python
class SmartDealRadar:
    """LLM-orchestrated deal evaluation using skill tools."""

    def __init__(self, llm, browser_manager, config):
        self.tools = [
            IdentifyItemTool(llm),
            VisualIdentifyTool(llm, config),
            EbayLookupTool(browser_manager),
            RetailLookupTool(),
            CategoryEstimateTool(llm),
        ]
        self.agent = create_tool_calling_agent(llm, self.tools, ORCHESTRATOR_PROMPT)

    async def evaluate(self, listing: Listing) -> Deal | None:
        """Let the LLM orchestrate skill calls to evaluate a listing."""
        result = await self.agent.ainvoke({
            "title": listing.title,
            "price": listing.price,
            "description": listing.description,
            "image_urls": listing.image_urls,
        })
        return self._parse_evaluation(result, listing)
```

### Orchestrator Prompt (skills/prompts.py)

```
You are a deal evaluation expert. You receive marketplace listings and must determine
if they are priced below market value.

You have these tools available:
- identify_item: Extract item details from title/description text
- visual_identify: Identify item from photos (use only when text is too vague)
- lookup_ebay_sold: Search eBay sold listings for real market prices
- lookup_retail_price: Find the MSRP/retail price of an item
- estimate_by_category: Get a price range by category (lowest confidence, use as last resort)

PROCESS:
1. First, call identify_item with the listing title and description.
2. If confidence < 0.5 or needs_visual is True, AND image_urls are available,
   call visual_identify.
3. With the identified item, call lookup_ebay_sold to get real sold prices.
4. If eBay returns < 3 results, also call lookup_retail_price for reference.
5. If both eBay and retail fail, use estimate_by_category as a fallback.
6. Compare the listing price to your market data and score the deal.

SCORING:
- FAIR: 0-20% below market (around average price)
- GOOD: 20-40% below market
- GREAT: 40-60% below market
- INCREDIBLE: 60%+ below market

FALSE POSITIVE CHECKS:
- If the listing price seems impossibly low (>80% discount), flag as potential scam
- If the item identification is uncertain, note it in your reasoning
- If visual_identify and identify_item disagree, lower your confidence
- Stock photos, vague descriptions, "DM for price" are red flags
- "For parts", "as-is", "broken" should dramatically lower market value comparison

REQUIRED: You must call at least one price lookup tool (lookup_ebay_sold,
lookup_retail_price, or estimate_by_category) before scoring. Never score based
solely on your training data.

Return your final evaluation as JSON:
{
    "item_identified": "exact item name",
    "identification_confidence": 0.0-1.0,
    "market_price": <float from lookup>,
    "price_source": "ebay_sold" | "retail" | "category_estimate",
    "deal_score": "fair" | "good" | "great" | "incredible" | "suspicious",
    "discount_pct": <float>,
    "reasoning": "<2-3 sentences>",
    "red_flags": ["list", "of", "concerns"] or []
}
```

### False Positive Prevention

| Scenario | How it's handled |
|----------|-----------------|
| Vague title ("Nice table - $50") | identify_item returns low confidence → visual_identify called → better ID |
| LLM misidentifies item | eBay lookup returns irrelevant results → LLM filters → low sample count → lower confidence |
| "PS5 controller" priced like full console | identify_item correctly identifies "controller" → eBay lookup for "PS5 controller" not "PS5" |
| Stock photos / scam listing | Red flags list includes "stock photos", "too good to be true" → score = "suspicious" |
| Broken/parts-only item | Condition detection in identify_item → eBay lookup filtered by condition → lower market price |
| No eBay data exists | Falls back to retail_price → then category_estimate → confidence reflects this |
| Visual and text ID disagree | Confidence lowered, disagreement noted in reasoning |

### Data Flow (One Listing Evaluation)

```
Listing: "Nice table $50" + [photo1.jpg, photo2.jpg]
  │
  ├─ LLM calls identify_item("Nice table", "")
  │   → {confidence: 0.3, category: "furniture/table", model: null, needs_visual: true}
  │
  ├─ LLM sees needs_visual=true, calls visual_identify([photo1.jpg, photo2.jpg])
  │   → {confidence: 0.8, item: "West Elm mid-century dining table", brand: "West Elm"}
  │
  ├─ LLM calls lookup_ebay_sold("West Elm mid-century dining table")
  │   → {median: $350, average: $370, count: 12, source: "ebay_sold"}
  │
  ├─ LLM evaluates: $50 vs $350 median = 86% discount
  │   Checks: 86% discount is very high → potential scam flag
  │   But: 12 eBay samples, confident visual ID, reasonable for local marketplace
  │
  └─ Returns: Deal(score=INCREDIBLE, market_price=$350, discount=86%,
                    reasoning="West Elm mid-century table typically sells for $350.
                    Listed at $50 (86% below market). High discount warrants verification
                    but strong sample size from eBay sold data.",
                    red_flags=["Very high discount - verify in person"])
```

## New Files

### Source
```
src/agentic_scraper/
  skills/
    __init__.py
    identify.py          # IdentifyItemTool - text-based item identification
    visual.py            # VisualIdentifyTool - image-based identification (fallback)
    ebay_lookup.py       # EbayLookupTool - eBay sold price lookup (hybrid HTTP/browser)
    retail_lookup.py     # RetailLookupTool - MSRP/retail price lookup
    category_estimate.py # CategoryEstimateTool - category-level pricing fallback
    models.py            # ItemIdentification, PriceLookupResult, CategoryEstimate
    prompts.py           # Orchestrator prompt, skill-specific prompts
    orchestrator.py      # SmartDealRadar - LLM-orchestrated evaluation
```

### Tests
```
tests/
  unit/
    test_identify_skill.py      # Text identification edge cases
    test_ebay_lookup.py         # HTTP parsing, fallback logic, price stats
    test_category_estimate.py   # Depreciation math, category pricing
    test_smart_deal_radar.py    # Orchestration flow, false positive guards
  integration/
    test_ebay_http_parser.py    # Real eBay HTML parsing (with fixtures)
    test_skill_orchestration.py # Full skill chain with mocked boundaries
```

## Config Additions

```python
# In AppConfig:
# --- Deal Radar v2 ---
deal_radar_version: str = "v2"           # "v1" (LLM-only) or "v2" (skill-based)
vision_model: str = ""                    # Separate vision model (empty = use main model)
ebay_lookup_enabled: bool = True          # Enable eBay sold price lookups
ebay_http_timeout_seconds: int = 10       # Timeout for direct HTTP scraping
ebay_min_samples: int = 3                 # Minimum sold listings for confident pricing
deal_radar_max_evaluations: int = 10      # Max listings to evaluate per scan cycle
deal_radar_scam_threshold_pct: float = 80 # Discount % that triggers scam warning
```

## Migration from v1

The old DealRadar in scanner/deal_radar.py becomes the v1 fallback. SmartDealRadar
in skills/orchestrator.py is v2. Controlled by `deal_radar_version` config:

- `v1`: Original LLM-only evaluation (fast, inaccurate) - kept for testing/comparison
- `v2`: Skill-based evaluation (slower, data-backed) - new default

ScanEngine checks config and uses the appropriate radar implementation.

## Implementation Order

1. Create skills/models.py - data classes for skill inputs/outputs
2. Create skills/identify.py - text-based identification (LLM structured output)
3. Create skills/ebay_lookup.py - HTTP scraper + browser-use fallback + price stats
4. Create skills/retail_lookup.py - web search for MSRP
5. Create skills/category_estimate.py - category-level pricing
6. Create skills/visual.py - image-based identification
7. Create skills/prompts.py - all prompts
8. Create skills/orchestrator.py - SmartDealRadar wiring it all together
9. Update scanner/engine.py to use SmartDealRadar when configured
10. Update config.py with new fields
11. Update main.py wiring
12. Tests throughout (TDD)

## Future Enhancements (not in this phase)

- **Price caching:** Store eBay lookup results in SQLite to avoid re-querying the same
  item. TTL of 7 days.
- **Keepa integration:** Amazon price history for new-in-box items (~$21/mo).
- **PriceCharting integration:** For games, cards, collectibles (~$6/mo).
- **Learning from feedback:** User reacts to deal alerts (thumbs up/down in Discord),
  which tunes the confidence thresholds over time.
- **CheapShark:** Free API for PC game deals (no key needed).
- **Multi-site price comparison:** Compare across FB Marketplace, eBay, Mercari, etc.
