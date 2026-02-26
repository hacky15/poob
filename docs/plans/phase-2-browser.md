# Phase 2: Browser Automation + Facebook Adapter

## Goal
Navigate Facebook Marketplace via browser-use, extract listings, and provide a pluggable site adapter system.

## Dependencies from Phase 1
- `AppConfig` (config.py) - browser_headless, browser_profiles_dir, browser_use_vision, stealth delays
- `Listing` (storage/models.py) - target dataclass for parsed listings
- `LLMProvider` (llm/provider.py) - provides BaseChatModel for agent tasks

## Scope

### 1. Stealth Utilities (browser/stealth.py)
- `random_delay(min_ms, max_ms)` - async sleep with uniform random duration
- `random_scroll_pattern()` - returns list of (direction, pixels, pause_ms) tuples
- `add_jitter(seconds, pct=0.15)` - adds +/- pct randomness to a target duration

### 2. Browser Manager (browser/manager.py)
- `BrowserManager` class with lifecycle management
- Constructor takes: headless, profiles_dir, use_vision, stealth config
- `start()` - launches browser-use Browser with persistent profile config
- `stop()` - gracefully closes browser
- `create_agent(task, llm, use_vision)` - creates ephemeral browser-use Agent
- Cookie persistence via BrowserContextConfig.cookies_file
- Context manager support (`async with`)

### 3. Site Adapter Protocol (sites/base.py)
- `ScanQuery` frozen dataclass: keywords, max_price, location, radius_miles, category
- `ScanResult` frozen dataclass: listings, raw_page_content, screenshot_b64, errors, duration
- `SiteAdapter` protocol with:
  - Properties: site_name, base_url, requires_login
  - Methods: login(), scan(), get_listing_details()

### 4. Site Registry (sites/registry.py)
- `SiteRegistry` class
- `discover()` - walks sites/ subdirectories, imports modules with `Adapter` export
- `get(site_name)` - retrieve adapter by name
- `list_sites()` - return all registered adapter names
- Auto-runs on construction

### 5. Facebook Marketplace Adapter (sites/facebook/)
- `adapter.py` - `FacebookMarketplaceAdapter` implementing SiteAdapter protocol
  - login() - navigates to FB, uses cookies if available, manual login fallback
  - scan() - builds prompt from query, creates agent, runs, parses results
  - get_listing_details() - navigates to single listing URL, extracts full details
- `prompts.py` - LLM task prompts with template variables:
  - SEARCH_PROMPT - navigate to marketplace, search, apply filters, extract listings
  - DETAIL_PROMPT - navigate to listing URL, extract full details
- `parser.py` - `parse_listings(agent_output)` and `parse_listing_detail(agent_output)`
  - Extract JSON from agent text responses (handles markdown code fences)
  - Convert to Listing dataclass instances
  - Graceful handling of malformed/partial responses

## Tests (written first)

### tests/unit/test_stealth.py (~8 tests)
- random_delay returns within bounds
- random_delay actually sleeps (mock asyncio.sleep)
- random_scroll_pattern returns list of tuples with correct structure
- random_scroll_pattern has varied values (not all identical)
- add_jitter returns within +/- pct bounds
- add_jitter with zero pct returns exact value
- add_jitter with custom pct

### tests/unit/test_site_registry.py (~6 tests)
- Registry discovers adapters from sites/ subdirectories
- Registry skips directories without __init__.py
- Registry skips modules without Adapter export
- get() returns correct adapter by name
- get() returns None for unknown site
- list_sites() returns all registered names

### tests/integration/test_facebook_adapter.py (~8 tests)
- Parser extracts listings from valid JSON array
- Parser handles empty JSON array
- Parser handles malformed JSON gracefully
- Parser extracts listings from markdown-fenced JSON
- Parser handles partial listing data (missing fields)
- Adapter builds correct prompt from ScanQuery
- Adapter produces ScanResult from mock agent run
- Adapter handles agent failure gracefully

## Verification
- `pytest tests/unit/test_stealth.py tests/unit/test_site_registry.py tests/integration/test_facebook_adapter.py` passes
- All 49 existing Phase 1 tests still pass
- Total target: ~71 tests
