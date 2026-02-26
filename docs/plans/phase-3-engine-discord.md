# Phase 3: Scan Engine + Discord Bot

## Goal
Full loop: scheduled scan -> detect watchlist matches -> notify via Discord.
Two-way Discord bot with commands to manage watches, trigger scans, and view status.

## Dependencies from Phase 1-2
- `AppConfig` (config.py) - scan_interval_minutes, discord_bot_token, discord_deals_channel_id
- `Listing`, `WatchItem`, `Deal`, `ScanLog`, `DealScore` (storage/models.py)
- All 4 repositories (storage/repositories/)
- `SiteAdapter`, `ScanQuery`, `ScanResult` (sites/base.py)
- `SiteRegistry` (sites/registry.py)
- `BrowserManager` (browser/manager.py)
- `LLMProvider` (llm/provider.py)

## Scope

### 1. WatchlistMatcher (scanner/watchlist.py)
- `build_queries(watch_items, sites)` - converts active WatchItems to ScanQuery list
- `match(listings, watch_items)` - for each listing × watch_item:
  - Keyword overlap (case-insensitive substring match)
  - Price <= max_price (if set)
  - Returns list of Deal objects with score based on discount percentage

### 2. ScanEngine (scanner/engine.py)
- `ScanEngine` class - the heart of the app
- Constructor takes: registry, browser_manager, llm, matcher, repos (listing, watchlist, deal, scan_log), notifier
- `run_scan_cycle()`:
  1. Get active watch items from WatchlistRepository
  2. Build queries via WatchlistMatcher
  3. For each (site_adapter, query): run scan, dedup, match, persist, notify
  4. Log ScanLog for each scan

### 3. ScanScheduler (scanner/scheduler.py)
- `ScanScheduler` class with asyncio-based timing
- `start()` - begins the scan loop
- `stop()` - stops the loop
- `pause()` / `resume()` - pause/resume without stopping
- `trigger_now()` - run an immediate scan
- Configurable interval with jitter

### 4. Discord Formatter (discord_bot/formatter.py)
- `format_deal_embed(deal, listing)` - builds Discord Embed for a deal alert
- `format_listing_embed(listing)` - builds embed for a single listing
- `format_watchlist_embed(watch_items)` - builds embed showing user's watches
- `format_status_embed(scheduler_state, last_scan, sites)` - bot status embed
- Color coding: grey=FAIR, green=GOOD, gold=GREAT, red=INCREDIBLE

### 5. DealNotifier (discord_bot/notifier.py)
- `DealNotifier` class
- `send_deal(deal, listing, channel)` - formats and sends deal embed
- `send_batch(deals_with_listings, channel)` - sends multiple deals

### 6. ScraperBot (discord_bot/bot.py)
- `ScraperBot(commands.Bot)` - main bot class
- Holds references to: config, scan_engine, scan_scheduler, repos, notifier
- `setup_hook()` - loads all cogs
- `on_ready()` - logs bot startup

### 7. Discord Cogs (discord_bot/cogs/)
- **watchlist_cog.py**: !watch, !unwatch, !watchlist
- **scanning_cog.py**: !scan, !pause, !resume, !status
- **search_cog.py**: !search, !deals
- **admin_cog.py**: !config, !sites, !logs

### 8. Wire main.py
- Update startup() to create all components and run bot + scheduler concurrently

## Tests (written first)

### tests/unit/test_watchlist_matcher.py (~10 tests)
- build_queries from watch items
- match: keyword matches title
- match: keyword case-insensitive
- match: no match when keywords don't overlap
- match: price within budget creates deal
- match: price over budget excluded
- match: no max_price always matches on price
- match: calculates discount percentage
- match: assigns correct DealScore based on discount

### tests/unit/test_formatter.py (~8 tests)
- deal embed has correct title, color, fields
- deal embed color coded by score
- listing embed has title, price, location
- watchlist embed lists all items
- status embed shows scheduler state
- deal embed handles missing image gracefully
- deal embed includes link to listing

### tests/integration/test_scan_engine.py (~8 tests)
- run_scan_cycle with no active watches does nothing
- run_scan_cycle calls adapter.scan for each query
- run_scan_cycle deduplicates existing listings
- run_scan_cycle saves new listings to repo
- run_scan_cycle creates deals from matches
- run_scan_cycle calls notifier for each deal
- run_scan_cycle logs ScanLog entry
- run_scan_cycle handles adapter errors gracefully

### tests/integration/test_discord_commands.py (~6 tests)
- !watch creates a watch item in DB
- !unwatch removes a watch item
- !watchlist returns user's watches
- !status returns scanner status
- !scan triggers immediate scan
- !sites lists registered adapters

## Verification
- All tests pass (target ~110+)
- Discord bot comes online, responds to !status
- !watch "test item" --max-price 100 persists to DB
- !watchlist shows the item
