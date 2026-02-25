# Agentic Web Scraper - Master Plan

> The single comprehensive document covering the entire project: origins, research, decisions, architecture, implementation details, and roadmap.

---

## 1. Project Origin & Vision

### What We're Building
An autonomous deal-hunting bot that runs on your machine, browses marketplace sites like a human (starting with Facebook Marketplace), identifies great deals using a local LLM, and alerts you on Discord. You interact with it from your phone via Discord - telling it what to watch for, triggering scans, and receiving deal alerts with images, prices, and direct links.

### How This Started
The initial question: "Is there an AI tool that can surf a site like Facebook Marketplace and find the best deal on an item close to me?"

Research into the current landscape revealed:

- **Anthropic's Computer Use (Claude)** - Can visually see a screen, move cursor, click, type. Uses your actual browser so it bypasses Facebook's anti-bot detection. BUT requires API key + pay-per-token. Not available on Pro subscription alone.
- **OpenAI's ChatGPT Agent (Operator)** - Spins up its own browser. Smart but hits a wall with Facebook's login requirements since it uses a separate cloud browser.
- **Gemini & Grok** - Better for static sites and news, not for dynamic platforms like Facebook.
- **Swoopa / Apify** - Purpose-built deal-finding tools, but proprietary and limited.

The decision: **Build our own** using open-source tools, running locally, with a local LLM to avoid API costs.

### Key Requirements from User
1. **Phone-operable** - Must be usable entirely from phone via Discord
2. **Two-way interaction** - Not just alerts; able to send commands ("watch for X under $Y")
3. **Autonomous deal radar** - Should generally look for "must-get deals" on its own, not just targeted searches
4. **Fully pluggable sites** - Start with Facebook Marketplace, but designed so any site can be added as a modular "skill"
5. **Full visibility** - Each connector/adapter is modular with clear, readable code
6. **Local LLM** - Use Ollama for privacy and zero API cost
7. **Discord** - Primary notification and command channel
8. **Clean codebase** - TDD, plans before code, industry best practices

---

## 2. Technology Research & Findings

### 2.1 Browser Automation: `browser-use`

**What it is:** The leading open-source library for turning an LLM into a web-browsing agent. Uses Playwright under the hood.

**How it works:**
- Extracts a simplified DOM representation from the page
- Identifies interactive elements (links, buttons, inputs), assigns index numbers
- Feeds this compressed representation to the LLM
- LLM decides what to click/type/scroll and issues structured actions (`click(index=5)`, `type(index=3, text="hello")`)
- Also supports **vision mode** - takes screenshots and sends them to vision-capable LLMs

**Key details:**
- **Repo:** https://github.com/browser-use/browser-use
- **Install:** `pip install browser-use` + `playwright install`
- **Installed version:** 0.11.12 (as of our install)
- **LLM integration:** Uses LangChain's chat model interface. Supports `ChatOllama`, `ChatOpenAI`, `ChatAnthropic`, or any LangChain-compatible model.
- **Persistent profiles:** Supports user data directories and cookies files so login state persists across sessions. Can also connect to an already-running Chrome instance via remote debugging.
- **Vision mode:** Can take screenshots and send to vision-capable models. Enabled via `use_vision=True` on the Agent.

**API pattern:**
```python
from browser_use import Agent, Browser, BrowserConfig, BrowserContextConfig
from langchain_ollama import ChatOllama

browser = Browser(config=BrowserConfig(
    headless=False,
    new_context_config=BrowserContextConfig(
        cookies_file="browser_profiles/facebook/cookies.json",
        browser_window_size={"width": 1280, "height": 1100},
    ),
))

llm = ChatOllama(model="llama3.1:8b", base_url="http://localhost:11434")

agent = Agent(
    task="Go to Facebook Marketplace and search for 'PS5' under $300",
    llm=llm,
    browser=browser,
    use_vision=True,
    max_actions_per_step=3,
    max_failures=3,
)

result = await agent.run()
```

**Facebook Marketplace challenges:**
- Aggressive bot detection (CAPTCHAs, verification prompts, temp bans)
- Obfuscated class names in DOM, deeply nested structures
- Dynamic loading, lazy-loaded content, login walls
- **Mitigations:** Persistent browser profile (already logged in), non-headless mode, human-like timing, vision mode where DOM parsing fails, slow navigation

### 2.2 Notification Channel: Discord

**Decision:** Discord (full bot via `discord.py`) over Telegram, ntfy.sh, Pushover, Twilio.

**Why Discord won:**
- Free
- Two-way communication (Cog system for commands + embeds for rich alerts)
- User already uses Discord
- Rich embeds with images, fields, colors - perfect for deal alerts
- Supports a bot that runs persistently and responds to commands

**Alternatives considered:**

| Service | Cost | Two-Way? | Why Not |
|---------|------|----------|---------|
| Telegram Bot | Free | Yes | Would work well, but user prefers Discord |
| ntfy.sh | Free | No | One-way only, no command support |
| Pushover | $5 one-time | No | One-way only |
| Twilio SMS | ~$0.008/msg | Yes (with extra work) | Expensive, overkill for self-notifications |

### 2.3 LLM Backend: Ollama (Local)

**Decision:** Local LLM via Ollama with a provider abstraction so cloud providers can be swapped in later.

**Setup:** Ollama runs at `http://localhost:11434`. LangChain integration via `langchain-ollama` package provides `ChatOllama` which is a standard `BaseChatModel`.

**Recommended models for browser automation:**
- **Qwen 2.5-VL (32B or 72B)** - Best for visual/spatial reasoning if VRAM allows
- **DeepSeek-R1** - Excellent for planning/reasoning chains
- **Llama 3.1/3.2** - Good general-purpose, lightweight option
- Default config: `llama3.1:8b` (adjustable via `.env`)

**Provider abstraction:** `LLMProvider` protocol allows swapping to `OpenAI`, `Anthropic`, or any LangChain-compatible provider without changing any other code.

---

## 3. Architecture

### 3.1 Tech Stack

| Layer | Choice | Package | Why |
|-------|--------|---------|-----|
| Browser automation | browser-use + Playwright | `browser-use>=0.3.0`, `playwright>=1.40.0` | LLM-driven browser control with DOM + vision |
| LLM | Ollama (local) | `langchain-ollama>=0.3.0`, `langchain-core>=0.3.0` | Free, private, LangChain-compatible |
| Discord | Full bot | `discord.py>=2.3.0` | Two-way: alerts + commands via Cogs |
| Database | SQLite (async) | `aiosqlite>=0.19.0` | Simple, no external DB, thin repo layer |
| Config | Typed env vars | `pydantic-settings>=2.1.0`, `pydantic>=2.5.0` | Type-safe config from .env |
| HTTP | Client | `httpx>=0.25.0` | Health checks, image fetching |
| Logging | Structured | `structlog>=23.2.0` | Console + file, JSON for production |
| Testing | TDD | `pytest>=7.4.0`, `pytest-asyncio>=0.23.0`, `pytest-cov`, `pytest-mock` | Async support, mocks at protocol boundaries |
| Linting | Fast + strict | `ruff>=0.1.0`, `mypy>=1.7.0` | Modern linter, strict type checking |

### 3.2 Project Structure

```
AgenticWebScraper/
├── .claude/
│   └── CLAUDE.md                       # Project conventions (auto-loaded by Claude Code)
├── pyproject.toml                      # Dependencies, tool configs (ruff, mypy, pytest)
├── .env.example                        # Template for secrets
├── .env                                # Actual secrets (gitignored)
├── .gitignore
│
├── docs/
│   ├── plans/                          # Feature implementation plans (written BEFORE code)
│   │   ├── master-plan.md             # THIS FILE - the comprehensive plan
│   │   └── phase-1-foundation.md      # Phase 1 detailed plan
│   └── references/                     # Reference materials for 3rd-party tools
│       ├── browser-use-api.md         # browser-use library reference
│       ├── ollama-setup.md            # Ollama model setup instructions
│       └── discord-bot-setup.md       # Discord bot token + channel setup
│
├── src/
│   └── agentic_scraper/
│       ├── __init__.py                 # Package init, __version__ = "0.1.0"
│       ├── main.py                     # Entry point, wires all components, starts bot + scheduler
│       ├── config.py                   # AppConfig (pydantic-settings, loads .env)
│       │
│       ├── llm/                        # LLM provider abstraction
│       │   ├── __init__.py
│       │   ├── provider.py             # LLMProvider protocol + create_llm_provider() factory
│       │   └── ollama_provider.py      # OllamaProvider: ChatOllama wrapper + health check
│       │
│       ├── browser/                    # Browser automation layer
│       │   ├── __init__.py
│       │   ├── manager.py             # BrowserManager: lifecycle, persistent profiles, agent creation
│       │   └── stealth.py             # Human-like delays, random scroll patterns, jitter
│       │
│       ├── sites/                      # Pluggable site adapters (each site is a subdirectory)
│       │   ├── __init__.py
│       │   ├── base.py                # SiteAdapter protocol, ScanQuery, ScanResult dataclasses
│       │   ├── registry.py            # SiteRegistry: auto-discovers adapters from subdirectories
│       │   └── facebook/              # Facebook Marketplace adapter
│       │       ├── __init__.py         # Exports: Adapter = FacebookMarketplaceAdapter
│       │       ├── adapter.py          # FacebookMarketplaceAdapter (implements SiteAdapter)
│       │       ├── prompts.py          # LLM navigation prompts for FB Marketplace
│       │       └── parser.py           # Extract Listing objects from agent output
│       │
│       ├── scanner/                    # Scan orchestration
│       │   ├── __init__.py
│       │   ├── engine.py              # ScanEngine: runs full scan cycle (the heart of the app)
│       │   ├── watchlist.py           # WatchlistMatcher: targeted search matching (fast, deterministic)
│       │   ├── deal_radar.py          # DealRadar: autonomous LLM-based deal scoring (slow, smart)
│       │   └── scheduler.py           # ScanScheduler: timing, intervals, pause/resume
│       │
│       ├── discord_bot/               # Discord two-way interface
│       │   ├── __init__.py
│       │   ├── bot.py                 # ScraperBot: discord.py Bot subclass, event hooks
│       │   ├── notifier.py            # DealNotifier: formats + sends deal embeds to channels
│       │   ├── formatter.py           # Embed builders for listings, deals, status
│       │   └── cogs/                  # Command groups (modular)
│       │       ├── __init__.py
│       │       ├── watchlist_cog.py   # !watch, !unwatch, !watchlist
│       │       ├── scanning_cog.py    # !scan, !pause, !resume, !status
│       │       ├── search_cog.py      # !search (one-shot), !deals (history)
│       │       └── admin_cog.py       # !config, !sites, !logs
│       │
│       ├── storage/                   # Persistence layer
│       │   ├── __init__.py
│       │   ├── database.py            # init_database(), init_schema() - SQLite with indexes
│       │   ├── models.py              # Dataclasses: Listing, WatchItem, Deal, ScanLog, DealScore
│       │   └── repositories/          # Thin CRUD layer over raw SQL
│       │       ├── __init__.py
│       │       ├── listing_repo.py    # ListingRepository: save, get, exists, list_recent
│       │       ├── watchlist_repo.py  # WatchlistRepository: save, get, list_for_user, list_active, delete
│       │       ├── deal_repo.py       # DealRepository: save, get, list_recent, list_unnotified, mark_notified
│       │       └── scan_log_repo.py   # ScanLogRepository: save, get, list_recent
│       │
│       └── utils/                     # Shared utilities
│           ├── __init__.py
│           ├── logging.py             # setup_logging() with structlog (console + file)
│           ├── retry.py               # Async retry decorator with exponential backoff
│           └── timing.py              # random_delay(), random_scroll_pattern(), add_jitter()
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                    # Shared fixtures: app_config, db_connection, mock_llm, samples
│   ├── unit/
│   │   ├── __init__.py
│   │   ├── test_config.py             # 11 tests: loading, defaults, validation, env vars
│   │   ├── test_models.py             # 20 tests: all dataclass fields, defaults, independence
│   │   ├── test_repositories.py       # 18 tests: all 4 repos CRUD with in-memory SQLite
│   │   ├── test_watchlist_matcher.py  # (Phase 3) keyword matching, price filtering
│   │   ├── test_deal_radar.py         # (Phase 4) LLM response parsing, scoring
│   │   ├── test_stealth.py            # (Phase 2) delay distributions within bounds
│   │   ├── test_formatter.py          # (Phase 3) Discord embed correctness
│   │   └── test_site_registry.py      # (Phase 2) adapter discovery
│   ├── integration/
│   │   ├── __init__.py
│   │   ├── test_scan_engine.py        # (Phase 3) full cycle with mocked adapter + notifier
│   │   ├── test_discord_commands.py   # (Phase 3) command dispatch + response format
│   │   └── test_facebook_adapter.py   # (Phase 2) mock agent → fixture data → parser extraction
│   └── e2e/
│       ├── __init__.py
│       └── test_full_pipeline.py      # (Phase 4) scan → detect → notify with all mocked boundaries
│
├── browser_profiles/                   # Persistent Playwright login sessions (gitignored)
│   └── facebook/
│       └── cookies.json
│
└── data/                               # Runtime data (gitignored)
    ├── scraper.db                      # SQLite database
    └── logs/                           # Structured log files
```

### 3.3 Key Abstractions

#### SiteAdapter Protocol (the plugin system)
```python
# src/agentic_scraper/sites/base.py

@dataclass(frozen=True)
class ScanQuery:
    keywords: str
    max_price: float | None = None
    location: str | None = None
    radius_miles: int | None = None
    category: str | None = None

@dataclass(frozen=True)
class ScanResult:
    listings: list[Listing]
    raw_page_content: str | None = None
    screenshot_b64: str | None = None
    errors: list[str]
    scan_duration_seconds: float

@runtime_checkable
class SiteAdapter(Protocol):
    @property
    def site_name(self) -> str: ...       # "facebook_marketplace"
    @property
    def base_url(self) -> str: ...        # "https://facebook.com/marketplace"
    @property
    def requires_login(self) -> bool: ... # True for Facebook

    async def login(self, browser_manager: BrowserManager) -> bool: ...
    async def scan(self, query: ScanQuery, browser_manager: BrowserManager, llm: BaseChatModel) -> ScanResult: ...
    async def get_listing_details(self, listing_url: str, browser_manager: BrowserManager, llm: BaseChatModel) -> Listing: ...
```

**To add a new site:**
1. Create `src/agentic_scraper/sites/<site_name>/` directory
2. Implement `adapter.py` satisfying the protocol
3. Implement `prompts.py` with LLM navigation prompts specific to that site
4. Implement `parser.py` for extracting Listing objects from agent output
5. Export `Adapter = YourAdapter` in `__init__.py`
6. SiteRegistry auto-discovers it on startup - zero config changes needed

#### LLMProvider Protocol (swappable brains)
```python
# src/agentic_scraper/llm/provider.py

@runtime_checkable
class LLMProvider(Protocol):
    @property
    def chat_model(self) -> BaseChatModel: ...  # LangChain-compatible
    @property
    def model_name(self) -> str: ...
    def is_available(self) -> bool: ...

def create_llm_provider(config: AppConfig) -> LLMProvider:
    if config.llm_provider == "ollama":
        return OllamaProvider(model=config.ollama_model, ...)
    # Future: "openai", "anthropic"
    raise ValueError(f"Unknown LLM provider: {config.llm_provider}")
```

#### SiteRegistry (auto-discovery)
```python
# src/agentic_scraper/sites/registry.py

class SiteRegistry:
    def discover(self) -> None:
        """Walk sites/ subdirectories, import each, register Adapter class."""
        sites_dir = Path(__file__).parent
        for child in sorted(sites_dir.iterdir()):
            if child.is_dir() and (child / "__init__.py").exists():
                module = import_module(f"agentic_scraper.sites.{child.name}")
                if hasattr(module, "Adapter"):
                    adapter = module.Adapter()
                    self._adapters[adapter.site_name] = adapter
```

#### BrowserManager (lifecycle + agent creation)
```python
# src/agentic_scraper/browser/manager.py

class BrowserManager:
    async def start(self) -> None:
        """Launch browser with stealth-optimized config + persistent cookies."""

    async def stop(self) -> None:
        """Gracefully close browser."""

    def create_agent(self, task: str, llm: BaseChatModel, use_vision: bool = True) -> Agent:
        """Create a fresh browser-use Agent bound to our managed browser."""
```

**Key design:** Browser is long-lived (one instance per session). Agents are ephemeral (one per scan task). This matches browser-use's design - Agents accumulate action history and should be discarded after each task completes.

#### Data Models
```python
# src/agentic_scraper/storage/models.py

class DealScore(Enum):
    UNKNOWN = "unknown"
    FAIR = "fair"           # Around market price
    GOOD = "good"           # 20-40% below market
    GREAT = "great"         # 40-60% below market
    INCREDIBLE = "incredible"  # 60%+ below market

@dataclass
class Listing:
    id, site, external_id, title, price, currency, description,
    location, seller_name, image_urls, listing_url, posted_at,
    scraped_at, raw_data

@dataclass
class WatchItem:
    id, keywords, max_price, location, radius_miles, category,
    sites, is_active, created_at, discord_user_id, discord_channel_id

@dataclass
class Deal:
    id, listing_id, watch_item_id, score, estimated_market_price,
    discount_pct, llm_reasoning, notified, notified_at, created_at

@dataclass
class ScanLog:
    id, site, query_keywords, listings_found, deals_found,
    errors, duration_seconds, started_at, completed_at
```

### 3.4 Data Flow (One Scan Cycle)

```
ScanScheduler timer fires (every N minutes, configurable)
  │
  ▼
ScanEngine.run_scan_cycle()
  │
  ├── WatchlistMatcher.build_queries()
  │     Reads all active WatchItem rows from SQLite
  │     Converts each to a ScanQuery(keywords, max_price, location, ...)
  │     Returns list[ScanQuery]
  │
  ├── FOR EACH (site_adapter, query) pair:
  │     │
  │     ├── SiteAdapter.scan(query, browser_manager, llm)
  │     │     │
  │     │     ├── BrowserManager.create_agent(task=prompt, llm=llm)
  │     │     │     Creates browser-use Agent with site-specific prompt
  │     │     │     Prompt comes from sites/<site>/prompts.py
  │     │     │
  │     │     ├── Agent.run()
  │     │     │     browser-use navigates to marketplace
  │     │     │     Types search query, applies filters
  │     │     │     Scrolls through results (with stealth delays)
  │     │     │     Extracts listing data via DOM + optional vision
  │     │     │     Returns AgentHistoryList
  │     │     │
  │     │     ├── sites/<site>/parser.py :: parse_listings()
  │     │     │     Takes raw agent output
  │     │     │     Parses into list[Listing] dataclasses
  │     │     │
  │     │     └── Returns ScanResult(listings, errors, duration)
  │     │
  │     ├── DEDUPLICATION
  │     │     For each listing, check ListingRepository.exists(site, external_id)
  │     │     Save new listings to SQLite
  │     │
  │     ├── WATCHLIST MATCHING (fast, deterministic)
  │     │     WatchlistMatcher.match(new_listings)
  │     │     For each listing × each active WatchItem:
  │     │       - Keyword overlap (fuzzy match)
  │     │       - Price <= max_price
  │     │       - Location proximity (if set)
  │     │     Produces Deal objects with score based on discount %
  │     │
  │     ├── DEAL RADAR (slow, LLM-powered, autonomous)
  │     │     DealRadar.evaluate(new_listings, llm)
  │     │     Constructs prompt: "Is this priced below market value?"
  │     │     LLM returns: {score, market_price, reasoning}
  │     │     Produces Deal objects for items scoring GOOD or better
  │     │
  │     ├── MERGE + PERSIST
  │     │     Combine watchlist_deals + radar_deals
  │     │     Deduplicate by listing_id (keep best score)
  │     │     Save each Deal to SQLite
  │     │
  │     └── NOTIFICATION
  │           DealNotifier.send_deal_alert(deal)
  │           Looks up full Listing data
  │           Builds Discord Embed (title, price, image, score, link, reasoning)
  │           Sends to channel (from WatchItem or default deals channel)
  │           Marks deal.notified = True
  │
  └── Log ScanLog to SQLite for each (site, query) pair
```

**Stealth is threaded throughout:**
- `random_delay(min_ms, max_ms)` between every browser action
- `random_scroll_pattern()` for human-like scrolling
- `add_jitter(target_seconds)` adds +/- 15% randomness to scheduled intervals
- Non-headless browser mode (harder to detect than headless)
- Persistent cookies (no repeated logins that look suspicious)

### 3.5 Discord Command Structure

#### Bot Setup
```python
# src/agentic_scraper/discord_bot/bot.py
class ScraperBot(commands.Bot):
    def __init__(self, config: AppConfig) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.scan_engine: ScanEngine | None = None
        self.scan_scheduler: ScanScheduler | None = None

    async def setup_hook(self) -> None:
        await self.load_extension("agentic_scraper.discord_bot.cogs.watchlist_cog")
        await self.load_extension("agentic_scraper.discord_bot.cogs.scanning_cog")
        await self.load_extension("agentic_scraper.discord_bot.cogs.search_cog")
        await self.load_extension("agentic_scraper.discord_bot.cogs.admin_cog")
```

#### Full Command Reference

| Command | Cog | Parameters | Description |
|---------|-----|------------|-------------|
| `!watch` | Watchlist | `<keywords> [--max-price N] [--location L] [--radius R] [--site S]` | Add a new watch item to your list |
| `!unwatch` | Watchlist | `<watch_id>` | Remove a watch item |
| `!watchlist` | Watchlist | *(none)* | Show all your active watches as embed list |
| `!scan` | Scanning | `[--site S]` | Trigger an immediate scan cycle |
| `!pause` | Scanning | *(none)* | Pause the automatic scan scheduler |
| `!resume` | Scanning | *(none)* | Resume the automatic scan scheduler |
| `!status` | Scanning | *(none)* | Show scanner status (running/paused, last scan time, next scan time) |
| `!search` | Search | `<keywords> [--max-price N] [--site S]` | One-shot search, returns top 5 results immediately |
| `!deals` | Search | `[--count N] [--min-score S]` | Show recent deals from history |
| `!config` | Admin | `[key] [value]` | View or set config values |
| `!sites` | Admin | *(none)* | List all registered site adapters and their status |
| `!logs` | Admin | `[--count N]` | Show recent scan logs |

#### Deal Alert Embed Format
```
╔══════════════════════════════════════╗
║ [!!!] PlayStation 5 Disc Edition     ║  ← Title with score emoji
║ ────────────────────────────────     ║
║ Price: $250.00  │  Est. Market: $400 ║
║ Discount: 37% off                    ║
║ Location: Portland, OR               ║
║ Site: facebook_marketplace            ║
║ ────────────────────────────────     ║
║ Why it's a deal:                     ║
║ PS5 Disc typically sells for $400.   ║
║ This is priced at $250, 37% below.  ║
║ ────────────────────────────────     ║
║ [Image of PS5]                       ║
║ ────────────────────────────────     ║
║ Deal Score: GREAT                    ║
╚══════════════════════════════════════╝
```

Color-coded by score: grey (FAIR), green (GOOD), gold (GREAT), red (INCREDIBLE).

### 3.6 Configuration

#### .env File (secrets + overrides)
```ini
# Discord
DISCORD_BOT_TOKEN=your-bot-token-here
DISCORD_DEALS_CHANNEL_ID=123456789

# Ollama (local LLM)
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1:8b
LLM_TEMPERATURE=0.3

# Facebook credentials
FACEBOOK_EMAIL=your-email@example.com
FACEBOOK_PASSWORD=your-password

# Scanning
SCAN_INTERVAL_MINUTES=15

# Deal Radar
DEAL_RADAR_ENABLED=true
DEAL_RADAR_MIN_SCORE=good
```

#### AppConfig Class (typed, validated)
```python
class AppConfig(BaseSettings):
    # Discord
    discord_bot_token: str              # REQUIRED
    discord_deals_channel_id: int       # REQUIRED
    discord_command_prefix: str = "!"

    # LLM
    llm_provider: str = "ollama"        # "ollama" | "openai" | "anthropic"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    llm_temperature: float = 0.3

    # Browser
    browser_headless: bool = False
    browser_profiles_dir: Path = Path("browser_profiles")
    browser_use_vision: bool = True

    # Scanning
    scan_interval_minutes: int = 15
    scan_max_listings_per_query: int = 20
    scan_stealth_min_delay_ms: int = 1500
    scan_stealth_max_delay_ms: int = 4000

    # Deal Radar
    deal_radar_enabled: bool = True
    deal_radar_min_score: str = "good"

    # Storage
    database_path: Path = Path("data/scraper.db")
    log_dir: Path = Path("data/logs")
    log_level: str = "INFO"

    # Site Credentials
    facebook_email: str = ""
    facebook_password: str = ""
```

### 3.7 Database Schema

```sql
-- Listings table: every scraped item
CREATE TABLE listings (
    id TEXT PRIMARY KEY,
    site TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT, price REAL, currency TEXT, description TEXT,
    location TEXT, seller_name TEXT, image_urls TEXT (JSON),
    listing_url TEXT, posted_at TEXT, scraped_at TEXT, raw_data TEXT (JSON),
    UNIQUE(site, external_id)
);

-- Watch items: user's saved searches
CREATE TABLE watch_items (
    id TEXT PRIMARY KEY,
    keywords TEXT NOT NULL,
    max_price REAL, location TEXT, radius_miles INTEGER, category TEXT,
    sites TEXT (JSON), is_active INTEGER,
    created_at TEXT, discord_user_id TEXT, discord_channel_id TEXT
);

-- Deals: matched or flagged listings
CREATE TABLE deals (
    id TEXT PRIMARY KEY,
    listing_id TEXT NOT NULL, watch_item_id TEXT,
    score TEXT, estimated_market_price REAL, discount_pct REAL,
    llm_reasoning TEXT, notified INTEGER, notified_at TEXT, created_at TEXT
);

-- Scan logs: audit trail
CREATE TABLE scan_logs (
    id TEXT PRIMARY KEY,
    site TEXT NOT NULL, query_keywords TEXT,
    listings_found INTEGER, deals_found INTEGER,
    errors TEXT (JSON), duration_seconds REAL,
    started_at TEXT, completed_at TEXT
);

-- Indexes for common queries
CREATE INDEX idx_listings_site_external ON listings(site, external_id);
CREATE INDEX idx_watch_items_user ON watch_items(discord_user_id);
CREATE INDEX idx_watch_items_active ON watch_items(is_active);
CREATE INDEX idx_deals_notified ON deals(notified);
CREATE INDEX idx_deals_created ON deals(created_at);
CREATE INDEX idx_scan_logs_started ON scan_logs(started_at);
```

---

## 4. Key Design Decisions & Rationale

### 4.1 Protocol over ABC
Python Protocols enable structural subtyping: any class with the right methods satisfies the protocol without explicit inheritance. A site adapter doesn't need to import or inherit from anything in the core package. This keeps adapters completely decoupled - they can even live in separate packages.

### 4.2 Agent per Scan, Browser per Session
Each `SiteAdapter.scan()` call creates a fresh `Agent` with a task-specific prompt. The `Browser` instance is long-lived (managed by `BrowserManager`), but Agents are ephemeral. This matches browser-use's design: the Agent accumulates history during a task and should be discarded after.

### 4.3 Two-Pass Deal Detection
- **WatchlistMatcher** (pass 1): Fast, deterministic. Keyword fuzzy match + price comparison. Zero LLM cost.
- **DealRadar** (pass 2): Slow, expensive. One LLM call per listing. Autonomous - finds deals you didn't ask for.

Running them separately means you can disable the radar to save LLM tokens while still getting watchlist matches, and vice versa. Results are merged with deduplication.

### 4.4 aiosqlite over SQLAlchemy
For a single-user bot with SQLite, a full ORM is unnecessary complexity. Raw async SQL via aiosqlite with a thin repository layer (4 classes, ~80 lines each) keeps things simple. If the project ever needs PostgreSQL, the repository interfaces stay the same - only the SQL implementation changes.

### 4.5 discord.py over Webhooks
Since we need both inbound commands AND outbound notifications, discord.py with its Cog system gives us a unified bot. The discord-webhook library would only handle outbound and would require a separate mechanism for commands.

### 4.6 Separate prompts.py per Site
LLM task prompts are the most site-specific and frequently-tuned part of the system. Isolating them in their own file makes them easy to iterate on without touching adapter logic. A prompt might change 10 times before the adapter code changes once.

---

## 5. Implementation Phases

### Phase 1: Foundation ✅ COMPLETE
**Goal:** Project skeleton that runs, loads config, connects to SQLite, verifies LLM.

**What was built:**
- `pyproject.toml` with all deps + tool configs
- `.env.example`, `.gitignore`
- `CLAUDE.md` with project conventions
- `config.py` - AppConfig with 20+ typed settings
- `storage/models.py` - 4 dataclasses + DealScore enum
- `storage/database.py` - SQLite schema init with 6 indexes
- `storage/repositories/` - 4 repository classes with full CRUD
- `llm/provider.py` - LLMProvider protocol + factory
- `llm/ollama_provider.py` - ChatOllama wrapper + health check
- `utils/logging.py` - structlog setup (fixed for v25.x API change)
- `main.py` - Entry point that wires everything together

**Tests:** 49 passing (11 config + 20 models + 18 repositories)

**Gotchas encountered:**
- `setuptools.backends._legacy:_Backend` doesn't exist - use `setuptools.build_meta`
- structlog v25.x removed `get_level_from_name()` - use `logging.DEBUG/INFO/etc` directly

### Phase 2: Browser + Facebook Adapter (NEXT)
**Goal:** Can navigate Facebook Marketplace and extract listings.

**What to build:**
1. `browser/manager.py` - BrowserManager with persistent profiles, agent creation, cookie management
2. `browser/stealth.py` - `random_delay()`, `random_scroll_pattern()`, `add_jitter()`
3. `sites/base.py` - SiteAdapter protocol, ScanQuery, ScanResult dataclasses
4. `sites/registry.py` - SiteRegistry with auto-discovery from subdirectories
5. `sites/facebook/adapter.py` - FacebookMarketplaceAdapter implementing SiteAdapter
6. `sites/facebook/prompts.py` - Navigation prompts:
   ```python
   SEARCH_PROMPT = """
   Go to Facebook Marketplace at https://www.facebook.com/marketplace.
   Search for "{keywords}" in the search bar.
   {price_filter}
   {location_filter}
   Scroll through results and extract for each listing:
   - Title, Price, Location, Seller name, Image URL, Listing URL
   Return as JSON array.
   """
   ```
7. `sites/facebook/parser.py` - Parse agent output into `list[Listing]`

**Tests to write first:**
- `test_stealth.py` - Delay distributions within bounds
- `test_site_registry.py` - Discovery finds Facebook adapter
- `test_facebook_adapter.py` (integration) - Mock agent → fixture data → parser extracts correctly

**Verification:**
- Unit + integration tests pass
- Manual: `BrowserManager.create_agent()` opens a Chromium window
- Manual: Facebook adapter navigates to Marketplace search page

### Phase 3: Scan Engine + Discord Bot
**Goal:** Full loop: scheduled scan -> detect deals -> notify via Discord.

**What to build:**
1. `scanner/watchlist.py` - WatchlistMatcher: build_queries(), match()
2. `scanner/engine.py` - ScanEngine: run_scan_cycle() orchestrating everything
3. `scanner/scheduler.py` - ScanScheduler: asyncio-based timing with pause/resume
4. `discord_bot/bot.py` - ScraperBot setup, event hooks
5. `discord_bot/formatter.py` - Embed builders for deals, listings, status
6. `discord_bot/notifier.py` - DealNotifier: format + send deal embeds
7. `discord_bot/cogs/watchlist_cog.py` - !watch, !unwatch, !watchlist
8. `discord_bot/cogs/scanning_cog.py` - !scan, !pause, !resume, !status
9. `discord_bot/cogs/search_cog.py` - !search, !deals
10. `discord_bot/cogs/admin_cog.py` - !config, !sites, !logs
11. Wire everything in `main.py`:
    ```python
    async def startup():
        config, db, llm, browser, registry = ... # init
        engine = ScanEngine(registry, browser, llm, matcher, radar, repos, notifier)
        scheduler = ScanScheduler(engine, config)
        bot = ScraperBot(config)
        bot.scan_engine = engine
        bot.scan_scheduler = scheduler
        await asyncio.gather(bot.start(token), scheduler.run())
    ```

**Tests to write first:**
- `test_watchlist_matcher.py` - Keyword matching, price filtering, location
- `test_formatter.py` - Embed field correctness, color coding
- `test_scan_engine.py` (integration) - Full cycle with mocked adapter + notifier
- `test_discord_commands.py` (integration) - Command parsing + response format

**Verification:**
- All tests pass
- Discord bot comes online, responds to `!status`
- `!watch "test item" --max-price 100` persists to DB
- `!watchlist` shows the item

### Phase 4: Deal Radar + Polish
**Goal:** Autonomous deal detection, error handling, E2E tests.

**What to build:**
1. `scanner/deal_radar.py` - DealRadar with LLM evaluation prompt:
   ```python
   EVALUATION_PROMPT = """
   Analyze this marketplace listing:
   Title: {title}
   Price: ${price}
   Description: {description}

   Respond in JSON:
   {
       "estimated_market_price": <float>,
       "deal_score": "fair" | "good" | "great" | "incredible",
       "reasoning": "<1-2 sentences>"
   }
   """
   ```
2. `utils/retry.py` - Async retry with exponential backoff
3. Harden error handling in ScanEngine (catch + log, don't crash)
4. Persistent cookie management per site
5. Graceful shutdown handling

**Tests to write first:**
- `test_deal_radar.py` - LLM response parsing, score calculation, edge cases (malformed JSON, timeout)
- `test_full_pipeline.py` (E2E) - Scan → match → notify with all components mocked at boundaries

**Verification:**
- Full E2E test passes
- Bot runs a complete scan cycle and sends deal embed to Discord
- Deal Radar correctly identifies underpriced items

---

## 6. Testing Strategy

### Principles
- **Test-first (TDD):** Write the test file BEFORE the implementation file for every module
- **No live LLM or browser in unit/integration tests.** Mocks and fixtures exclusively
- **In-memory SQLite** for all repository tests (`:memory:`)
- **pytest-asyncio** with `asyncio_mode = "auto"` so async tests just work

### Mocking Boundaries
The critical architectural decision: **where the mock boundary sits**

| Test Layer | What's Mocked | What's Real |
|-----------|---------------|-------------|
| Repository tests | Nothing | SQLite (in-memory), models, repos |
| ScanEngine tests | SiteAdapter.scan(), DealNotifier | Engine logic, repos, matcher |
| SiteAdapter tests | Agent.run() | Adapter logic, prompts, parser |
| DealRadar tests | BaseChatModel.ainvoke() | Radar logic, prompt building |
| Discord tests | discord.TextChannel.send() | Cog logic, command parsing |

Each layer is testable in isolation. Integration tests verify the wiring between layers.

### Shared Fixtures (tests/conftest.py)
- `app_config` - Config with test defaults in tmp_path
- `db_connection` - In-memory SQLite with schema initialized
- `mock_llm` - MagicMock with AsyncMock ainvoke
- `mock_browser_manager` - AsyncMock that doesn't launch a real browser
- `sample_listing` - Realistic PS5 listing
- `sample_watch_item` - PS5 watch with $300 max
- `sample_deal` - GREAT score deal
- `sample_scan_log` - FB Marketplace scan log

### Current Test Count
- Phase 1: **49 tests passing** (11 config + 20 models + 18 repos)
- Phase 2 target: ~65 tests
- Phase 3 target: ~95 tests
- Phase 4 target: ~115 tests

---

## 7. Security Considerations

- **Credentials:** Facebook email/password + Discord token stored ONLY in `.env` (gitignored)
- **Browser profiles:** Cookie files in `browser_profiles/` (gitignored) - contain session tokens
- **No third-party skills/plugins:** All site adapters written by us, not downloaded from registries
- **Facebook ToS:** Automated access is against Facebook's terms. Mitigated by human-like behavior, persistent login, slow navigation. User assumes responsibility.
- **Rate limiting:** Configurable delays between actions, jitter on scan intervals
- **No data exfiltration:** Everything runs locally, no data sent to external services except Discord alerts

---

## 8. Running the App

### First-time setup
```bash
# Clone and enter project
cd AgenticWebScraper/AgenticWebScraper

# Create and activate venv
python3 -m venv venv
source venv/bin/activate

# Install in dev mode
pip install -e ".[dev]"

# Install browser binaries
playwright install

# Configure
cp .env.example .env
# Edit .env with your Discord token, channel ID, Facebook creds

# Run tests
pytest

# Start the bot
python -m agentic_scraper.main
```

### Prerequisites
- Python 3.12+
- Ollama running locally (`ollama serve`)
- A model pulled (`ollama pull llama3.1:8b`)
- Discord bot created (token from Discord Developer Portal)
- A Discord server with a deals channel

---

## 9. Installed Dependencies (as of Phase 1)

Core packages installed:
- `browser-use==0.11.12` (brings in playwright, langchain-core, langchain-ollama, etc.)
- `discord.py==2.6.4`
- `pydantic==2.12.5`, `pydantic-settings==2.13.1`
- `aiosqlite==0.22.1`
- `structlog==25.5.0`
- `httpx==0.28.1`
- `pytest==9.0.2`, `pytest-asyncio==1.3.0`, `pytest-cov==7.0.0`, `pytest-mock==3.15.1`
- `ruff==0.15.2`, `mypy==1.19.1`

Notable transitive dependencies:
- `langchain-core==1.2.15`, `langchain-ollama==1.0.1`
- `playwright==1.58.0`
- `anthropic==0.83.0`, `openai==2.24.0` (pulled in by browser-use for multi-provider support)
