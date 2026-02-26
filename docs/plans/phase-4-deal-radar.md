# Phase 4: Deal Radar + Polish

## Goal
Autonomous deal detection using LLM evaluation, async retry utility, and E2E tests.

## Scope

### 1. DealRadar (scanner/deal_radar.py)
- `DealRadar` class - autonomous LLM-powered deal scoring
- `evaluate(listings, llm)` - evaluates each listing against market prices
- Constructs prompt asking LLM: "Is this priced below market value?"
- Parses JSON response: {estimated_market_price, deal_score, reasoning}
- Returns Deal objects for items scoring GOOD or better
- Handles malformed LLM responses gracefully

### 2. Retry Utility (utils/retry.py)
- `async_retry(max_retries, base_delay, max_delay)` decorator
- Exponential backoff with jitter
- Configurable exception types to retry on

### 3. ScanEngine Integration
- Add DealRadar as optional pass in run_scan_cycle()
- Controlled by config.deal_radar_enabled
- Merge radar deals with watchlist deals, dedup by listing_id

### 4. E2E Test
- Full pipeline: scan -> dedup -> match -> radar -> notify
- All external boundaries mocked (adapter, LLM, Discord)

## Tests (written first)

### tests/unit/test_deal_radar.py (~8 tests)
- evaluate returns deals for underpriced listings
- evaluate handles valid JSON response
- evaluate handles malformed JSON response
- evaluate handles empty response
- evaluate respects min_score filter
- evaluate assigns correct DealScore
- prompt includes listing title and price
- evaluate handles LLM timeout/error

### tests/e2e/test_full_pipeline.py (~4 tests)
- Full cycle: scan -> match -> notify with mocked boundaries
- Full cycle with deal radar enabled
- Full cycle with no matches produces no notifications
- Full cycle handles errors without crashing

## Verification
- All tests pass (target ~125+)
- DealRadar correctly identifies underpriced items
- Full E2E pipeline works end-to-end
