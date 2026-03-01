# Phase 7: Patrol System Overhaul

## Context

The current system is search-based: it builds keyword queries from watchlist items, runs them through FB Marketplace search, and evaluates results. This is fundamentally the wrong model. The user wants **continuous monitoring of ALL new Appleton-area listings** with deal detection, not targeted keyword searches.

Key findings from Google Deep Research:
- **No simultaneous sessions** - multi-tab/multi-context on one account gets flagged by Meta. Must serialize category checks with 15-25s delays.
- **15-30 min batching delay** - new listings don't appear instantly in FB's index. Polling faster than every 2 min is wasted.
- **Cache busting** - oscillate radius parameter between requests to force FB backend recalculation.
- **Structured data** - individual listing pages have Open Graph + JSON-LD in `<head>` (no JS rendering needed for detail extraction).
- **Appleton volume** - ~650 new listings/day, ~27/hour avg, ~65-70/hour peak (4-9PM).
- **Shadow blocking** - FB silently degrades results instead of hard-blocking.

## Architecture: 5-Phase Patrol Cycle

```
PatrolScheduler fires (adaptive interval: 2min peak, 5min moderate, 15min overnight)
  |
  v
PatrolEngine.run_patrol_cycle()
  |
  |-- Phase 1: CATEGORY SWEEP (~5 min)
  |     For each category in rotation (serialized, 15-25s delays):
  |       Build URL: /marketplace/category/{slug}?sortBy=creation_time_descend&daysSinceListed=1
  |       with cache-busting radius oscillation (20mi -> 22mi -> 18mi -> 24mi)
  |       Navigate -> scroll -> JS extract surface listings
  |
  |-- Phase 2: DEDUP (instant)
  |     Check listing_repo.exists(site, external_id) for each listing
  |     Save only truly new listings to DB
  |
  |-- Phase 3: DEEP INSPECTION (~1-2 min)
  |     For each new listing (3-7s delays between):
  |       Navigate to listing detail page
  |       Extract Open Graph meta tags + JSON-LD structured data
  |       Enrich Listing with full description, images, condition
  |
  |-- Phase 4: EVALUATION (~1-2 min, no browser needed)
  |     SmartDealRadar.evaluate() on each enriched listing
  |     InterestMatcher.match_single() against user interests
  |     Interest matches that SmartDealRadar missed -> FAIR score deal
  |
  |-- Phase 5: NOTIFY
  |     DealNotifier.send_deal() for each qualifying deal
  |     Mark deals as notified
```

## What's Reused (zero changes)

- `browser/manager.py` - BrowserManager, get_page(), start/stop
- `browser/page_actions.py` - navigate_and_wait(), scroll_page(), evaluate_js()
- `browser/stealth.py` - random_delay(), apply_scroll_pattern(), add_jitter()
- `sites/facebook/js_extractor.py` - EXTRACT_LISTINGS_JS (add 2 new snippets)
- `sites/facebook/parser.py` - parse_listings()
- `sites/facebook/categories.py` - CATEGORY_SLUG_MAP, get_category_slug()
- `skills/orchestrator.py` - SmartDealRadar v2 (entire pipeline)
- All 5 skill tools (identify, visual, ebay_lookup, retail_lookup, category_estimate)
- `storage/repositories/listing_repo.py` - zero changes
- `storage/repositories/deal_repo.py` - zero changes
- `discord_bot/notifier.py` - zero changes
- All LLM providers (ollama, gemini, cerebras)

## Implementation Steps (TDD)

### Step 1: Data Models + Config + Schema Migration
**Goal:** Rename `WatchItem.keywords` -> `.interest`, `ScanLog.query_keywords` -> `.category`, add patrol config fields, DB migration.

**Files to modify:**
- `src/agentic_scraper/storage/models.py` - field renames
- `src/agentic_scraper/config.py` - add 17 `patrol_*` fields
- `src/agentic_scraper/storage/database.py` - DDL updates + `migrate_schema()` for existing DBs
- `src/agentic_scraper/storage/repositories/watchlist_repo.py` - SQL column rename
- `src/agentic_scraper/storage/repositories/scan_log_repo.py` - SQL column rename
- `tests/conftest.py` - fixture updates (`keywords=` -> `interest=`, `query_keywords=` -> `category=`)
- `tests/unit/test_models.py` - field name updates
- `tests/unit/test_config.py` - add patrol config tests
- `tests/unit/test_repositories.py` - field name updates

**New config fields:**
```python
patrol_enabled: bool = True
patrol_categories: list[str] = ["electronics", "furniture", "vehicles",
                                 "sports", "garden", "appliances",
                                 "free", "toys", "apparel", "entertainment"]
patrol_base_radius_miles: int = 20
patrol_radius_jitter: int = 4
patrol_inter_category_delay_min_ms: int = 15000  # 15s
patrol_inter_category_delay_max_ms: int = 25000  # 25s
patrol_inter_listing_delay_min_ms: int = 3000    # 3s
patrol_inter_listing_delay_max_ms: int = 7000    # 7s
patrol_peak_interval_seconds: int = 120          # 2 min
patrol_moderate_interval_seconds: int = 300      # 5 min
patrol_offpeak_interval_seconds: int = 600       # 10 min
patrol_dead_interval_seconds: int = 900          # 15 min
patrol_peak_hours_start: int = 16                # 4 PM
patrol_peak_hours_end: int = 21                  # 9 PM
patrol_include_all_categories: bool = True
patrol_days_since_listed: int = 1
patrol_deep_inspect_enabled: bool = True
```

**DB migration:** `migrate_schema()` uses `PRAGMA table_info()` to detect old column names, then rebuilds tables. Safe for both fresh installs and existing DBs.

**Verify:** `pytest tests/unit/test_models.py tests/unit/test_config.py tests/unit/test_repositories.py`

---

### Step 2: InterestMatcher (rename from WatchlistMatcher)
**Goal:** Rename class, remove `build_queries()`, add `match_single()`, keep `watchlist.py` as compat shim.

**Files:**
- Create `src/agentic_scraper/scanner/interest_matcher.py` - `InterestMatcher` class
- Modify `src/agentic_scraper/scanner/watchlist.py` - thin shim: `from .interest_matcher import InterestMatcher as WatchlistMatcher`
- Create `tests/unit/test_interest_matcher.py` - full test suite

**Key changes:**
- `WatchlistMatcher` -> `InterestMatcher`
- Remove `build_queries()` (no more search-based queries)
- Add `match_single(listing, interests) -> list[Deal]`
- `_keywords_match(title, keywords)` -> `_interest_matches(title, interest)` (same algorithm)
- All `watch.keywords` refs -> `watch.interest`

**Verify:** `pytest tests/unit/test_interest_matcher.py`

---

### Step 3: Patrol URL Builder + PatrolScanner
**Goal:** Category sweep infrastructure - URL construction with cache busting.

**Files:**
- Create `src/agentic_scraper/sites/facebook/patrol_scanner.py`
  - `build_patrol_url(category, radius, days_since_listed)` -> URL
  - `RadiusOscillator` class (cycles through [20, 22, 18, 24] miles)
  - `PatrolScanner.sweep_category(page, category)` -> `list[Listing]`
- Create `tests/unit/test_patrol_scanner.py`

**URL pattern:** `/marketplace/category/{slug}?sortBy=creation_time_descend&daysSinceListed=1&deliveryMethod=local_pick_up&exact=false&radius={r}`

**Verify:** `pytest tests/unit/test_patrol_scanner.py`

---

### Step 4: Detail Extractor + OG/JSON-LD JS Snippets
**Goal:** Extract rich listing data from individual listing pages without LLM assistance.

**Files:**
- Modify `src/agentic_scraper/sites/facebook/js_extractor.py` - add `EXTRACT_OPEN_GRAPH_JS` and `EXTRACT_JSON_LD_JS`
- Create `src/agentic_scraper/sites/facebook/detail_extractor.py`
  - `extract_listing_details(page, listing) -> Listing` (enriches with OG + JSON-LD data)
- Create `tests/unit/test_detail_extractor.py`

**OG tags extracted:** `og:title`, `og:description`, `og:image`, `og:url`
**JSON-LD extracted:** price, condition, availability (schema.org Product/Offer)

**Verify:** `pytest tests/unit/test_detail_extractor.py`

---

### Step 5: PatrolEngine
**Goal:** The 5-phase patrol cycle orchestrator.

**Files:**
- Create `src/agentic_scraper/scanner/patrol_engine.py`
  - `PatrolEngine.__init__(browser_manager, repos, notifier, interest_matcher, smart_deal_radar, config)`
  - `run_patrol_cycle() -> PatrolCycleResult`
  - `_sweep_categories(page, result)` - Phase 1
  - `_dedup_and_save(listings)` - Phase 2 (same logic as ScanEngine)
  - `_deep_inspect_listings(page, listings)` - Phase 3
  - `_evaluate_listings(listings)` - Phase 4
  - `_notify_deals(deals, listings)` - Phase 5
- Create `tests/unit/test_patrol_engine.py`
- Create `tests/integration/test_patrol_engine.py`

**Dependencies:** Steps 1-4 must be complete.

**Verify:** `pytest tests/unit/test_patrol_engine.py tests/integration/test_patrol_engine.py`

---

### Step 6: PatrolScheduler
**Goal:** Adaptive timing scheduler with same public interface as ScanScheduler (for Discord bot compat).

**Files:**
- Create `src/agentic_scraper/scanner/patrol_scheduler.py`
  - `PatrolScheduler.__init__(engine, config)`
  - Same interface: `start()`, `stop()`, `pause()`, `resume()`, `trigger_now()`
  - Same properties: `is_running`, `is_paused`, `last_scan_time`, `next_scan_time`
  - `_calculate_interval(hour)` - adaptive timing with Gaussian jitter
- Create `tests/unit/test_patrol_scheduler.py`

**Adaptive timing:**
| Time of Day | Base Interval | Description |
|---|---|---|
| 4-9 PM (+ weekends) | 120s (2 min) | Peak FB activity |
| 8 AM - 4 PM | 300s (5 min) | Moderate |
| 6-8 AM, 9 PM - midnight | 600s (10 min) | Off-peak |
| Midnight - 6 AM | 900s (15 min) | Dead zone |

Each interval gets Gaussian jitter +/- 20% (clamped to +/- 40%).

**Verify:** `pytest tests/unit/test_patrol_scheduler.py`

---

### Step 7: Discord + Agent Updates
**Goal:** Update user-facing text, implement `!deals`, remove `!search` stub.

**Files to modify:**
- `src/agentic_scraper/discord_bot/cogs/watchlist_cog.py` - `keywords` param -> `interest`
- `src/agentic_scraper/discord_bot/cogs/scanning_cog.py` - "scan" -> "patrol" in messages
- `src/agentic_scraper/discord_bot/cogs/search_cog.py` - implement `!deals` properly, remove `!search`
- `src/agentic_scraper/discord_bot/bot.py` - add `deal_repo`/`listing_repo` attrs for SearchCog
- `src/agentic_scraper/discord_bot/formatter.py` - `item.keywords` -> `item.interest`
- `src/agentic_scraper/agent/tools.py` - `keywords=` -> `interest=`, updated descriptions
- `src/agentic_scraper/agent/prompts.py` - "search/scan" -> "patrol" language
- Update `tests/integration/test_discord_commands.py`

**Verify:** `pytest tests/integration/test_discord_commands.py tests/unit/test_agent_tools.py`

---

### Step 8: Main.py Wiring
**Goal:** Replace ScanEngine/ScanScheduler with PatrolEngine/PatrolScheduler.

**File:** `src/agentic_scraper/main.py`
- Replace ScanEngine block with PatrolEngine construction
- Replace ScanScheduler with PatrolScheduler
- Add deal_repo/listing_repo to ScraperBot
- Remove unused imports (ScanEngine, ScanScheduler, WatchlistMatcher)
- Keep SiteRegistry for admin_cog health checks

**Verify:** Full test suite passes, import doesn't crash.

---

### Step 9: E2E + Polish
**Goal:** Update E2E tests, run full suite, fix regressions.

**Files:**
- Modify `tests/e2e/test_full_pipeline.py` - replace ScanEngine with PatrolEngine
- Run: `pytest tests/ -v --tb=short`
- Expected: ~184 existing tests pass + ~40-50 new tests = ~230 total

---

## File Change Summary

| Action | Files |
|--------|-------|
| **Create (6)** | `scanner/patrol_engine.py`, `scanner/patrol_scheduler.py`, `scanner/interest_matcher.py`, `sites/facebook/patrol_scanner.py`, `sites/facebook/detail_extractor.py` |
| **Create tests (5)** | `test_patrol_engine.py`, `test_patrol_scheduler.py`, `test_interest_matcher.py`, `test_patrol_scanner.py`, `test_detail_extractor.py` + integration test |
| **Modify (16)** | `models.py`, `config.py`, `database.py`, `watchlist_repo.py`, `scan_log_repo.py`, `watchlist.py`, `js_extractor.py`, `watchlist_cog.py`, `scanning_cog.py`, `search_cog.py`, `bot.py`, `formatter.py`, `tools.py`, `prompts.py`, `main.py`, all affected test files |
| **Retire (2)** | `scanner/engine.py`, `scanner/deal_radar.py` (kept in repo, no longer called) |

## Key Gotchas

1. **`WatchItem.keywords` -> `.interest` cascades to 15+ files** - must be Step 1, everything depends on it
2. **DB migration** - `migrate_schema()` detects old columns via PRAGMA, safe for fresh + existing DBs
3. **PatrolScheduler must match ScanScheduler interface** - ScanningCog duck-types the scheduler
4. **`RadiusOscillator._radii` must be instance var**, not class var (test isolation)
5. **`format="json"` on ChatOllama BREAKS browser-use** - patrol uses `get_page()` + JS, not browser-use agents, so this isn't an issue for the patrol path
6. **`listing_repo.save()` uses `ON CONFLICT DO UPDATE`** - safe to re-save enriched listings after deep inspection
7. **ScanEngine is retired, not deleted** - existing tests via compat shim still pass, no import breakage
