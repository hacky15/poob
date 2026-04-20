# Poob

Autonomous Discord companion with a deal-hunting scanner, conversational agent, voice chat, and music playback. The scanner patrols Facebook Marketplace on a schedule, evaluates deals through a multi-stage LLM + VLM pipeline, and sends notifications via Discord.

## What It Does

1. **Patrols** Facebook Marketplace on a configurable schedule (adaptive timing based on time of day)
2. **Triages** new listings with a cheap/fast cloud LLM to filter obvious junk
3. **Enriches** promising listings with reverse image search (Google Cloud Vision, SerpAPI Google Lens) to identify products that have vague titles
4. **Evaluates** deals using a vision-language model that sees the photos, comparable sold prices, and enrichment context
5. **Notifies** you via Discord DM (watchlist matches) or public channel (general deals)
6. **Converses** -- the Discord bot is a full conversational agent. Mention it to manage your watchlist, ask about recent deals, pause/resume scanning, or get status updates.

## Architecture

```
Discord Bot (commands + conversational agent)
    |
Patrol Scheduler (adaptive timing: peak/moderate/off-peak/dead hours)
    |
Patrol Engine (4-step cycle)
    |--- Sweep: browser automation via Playwright + GraphQL intercept
    |--- Dedup: batch dedup against SQLite, filter stale/sponsored
    |--- Evaluate: text triage -> visual enrichment -> VLM deal evaluation
    |--- Notify: public channel (top deals) + user DMs (watchlist matches)
    |
LLM Providers (cascading fallback chains)
    |--- Cloud text: Cerebras -> Groq -> local Ollama
    |--- Vision: Groq Vision -> Ollama VLM
    |--- VLM eval: Gemini Flash -> Groq Vision -> Gemini Pro -> OpenRouter -> Ollama
    |--- Agent brain: Groq -> NVIDIA NIM -> Gemini -> Ollama
    |--- Browser: NVIDIA NIM / Gemini / Ollama (configurable)
```

## Requirements

- Python 3.12+
- [Ollama](https://ollama.com/download) running locally
- A Discord bot token and channel ID
- At least one cloud LLM API key (Cerebras or Groq recommended -- both are free)

## Setup

```bash
# Clone and install
git clone <repo-url> && cd poob
pip install -e ".[dev]"
playwright install chromium

# Pull Ollama models
ollama pull qwen3:8b
ollama pull qwen3:4b
ollama pull qwen2.5vl:3b

# Configure
cp .env.example .env
# Edit .env with your API keys (see Provider Setup below)

# Run
python -m poob.main
```

## Provider Setup

The bot uses multiple free-tier AI providers with automatic fallback chains. You only need Ollama + at least one cloud provider to get started. More providers = better resilience and speed.

### Required

| Provider | Purpose | Setup |
|----------|---------|-------|
| **Ollama** | Local LLM (JSON extraction, fallback for everything) | Install from [ollama.com](https://ollama.com/download). Pull models listed above. |
| **Discord** | Bot interface | Create app at [discord.com/developers](https://discord.com/developers/applications). Enable Message Content intent. Add bot to your server. Set `DISCORD_BOT_TOKEN` and `DISCORD_DEALS_CHANNEL_ID`. |

### Cloud LLM (at least one recommended)

These power item identification, price extraction, text triage, and the conversational agent. The bot cascades through them in order: Cerebras -> Groq -> local Ollama.

| Provider | Free Tier | Setup |
|----------|-----------|-------|
| **Cerebras** | 1K RPM, 1M tokens/day | Get key at [cloud.cerebras.ai](https://cloud.cerebras.ai/). Set `CEREBRAS_API_KEY`. |
| **Groq** | 30 RPM, 14,400 RPD | Get key at [console.groq.com](https://console.groq.com/). Set `GROQ_API_KEY`. Also provides vision model for VLM fallback. |
| **Google Gemini** | 15 RPM, 1M TPD | Get key at [aistudio.google.com](https://aistudio.google.com/apikey). Set `GOOGLE_API_KEY`. Used for VLM evaluation and as agent brain fallback. |

### Browser Automation (optional cloud)

By default, browser-use runs on local Ollama. For faster/more reliable browser automation, configure one of these:

| Provider | Free Tier | Setup |
|----------|-----------|-------|
| **NVIDIA NIM** | 1K credits on signup | Get key at [build.nvidia.com](https://build.nvidia.com/). Set `NVIDIA_API_KEY` and `BROWSER_LLM_PROVIDER=nvidia`. |
| **Google Gemini** | (same key as above) | Set `BROWSER_LLM_PROVIDER=gemini`. Uses a separate model (`gemini-2.0-flash`) to avoid sharing rate limits with VLM. |

### Web Search (for comparable pricing)

Used to look up eBay sold prices and retail prices for deal scoring. Without these, the VLM still evaluates deals but without comparable sales data.

| Provider | Free Tier | Setup |
|----------|-----------|-------|
| **Tavily** | 1,000 searches/month | Get key at [tavily.com](https://tavily.com). Set `TAVILY_API_KEY`. |
| **Serper.dev** | 2,500 searches (one-time) | Get key at [serper.dev](https://serper.dev). Set `SERPER_API_KEY`. Fallback when Tavily quota runs out. |

### Visual Enrichment (optional)

Reverse image search identifies products from listing photos when the title is vague (e.g., "selling this $20" with a photo of a KitchenAid mixer).

| Provider | Free Tier | Setup |
|----------|-----------|-------|
| **Google Cloud Vision** | 1,000 calls/month | Enable the API at [GCP console](https://console.cloud.google.com/apis/library/vision.googleapis.com). Set `GOOGLE_CLOUD_VISION_API_KEY`. |
| **SerpAPI** | 250 searches/month | Get key at [serpapi.com](https://serpapi.com/). Set `SERPAPI_API_KEY`. Provides Google Lens product matching. |

### VLM Evaluation (optional extra providers)

The VLM evaluator cascades through providers: Gemini Flash -> Groq Vision -> Gemini Pro -> OpenRouter -> Ollama. Gemini and Groq are already covered above. OpenRouter adds one more fallback layer:

| Provider | Free Tier | Setup |
|----------|-----------|-------|
| **OpenRouter** | Free models available | Get key at [openrouter.ai](https://openrouter.ai/). Set `OPENROUTER_API_KEY`. |

### Facebook Login (optional)

Setting `FACEBOOK_EMAIL` and `FACEBOOK_PASSWORD` enables persistent login, which avoids login prompts and gives access to more listing data. The bot stores cookies in `browser_profiles/` after the first login.

## Discord Commands

Mention the bot or use the command prefix (default `!`) to interact:

- **Watchlist**: "watch for Herman Miller chairs under $200" / "remove Herman Miller from my watchlist"
- **Status**: "what's the patrol status?" / "how many deals today?"
- **Control**: "pause patrol" / "resume patrol" / "run a patrol now"
- **Exclusions**: "exclude IKEA" / "show my exclusions"
- **Feedback**: React to deal notifications with thumbs up/down to improve future results

## Configuration Reference

All configuration is via environment variables. See [.env.example](.env.example) for the full list with defaults. Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `SCAN_MODE` | `direct` | `direct` (CDP/fast) or `agent` (browser-use, slower) |
| `PATROL_BASE_RADIUS_MILES` | `40` | Search radius for Facebook Marketplace |
| `PATROL_PEAK_INTERVAL_SECONDS` | `300` | Scan interval during peak hours (4-9 PM) |
| `DEAL_RADAR_MIN_SCORE` | `good` | Minimum deal quality to store (fair/good/great/incredible) |
| `NOTIFICATION_MAX_PER_DAY` | `10` | Cap notifications per user per day |
| `LISTING_MAX_AGE_HOURS` | `6` | Drop listings older than this from evaluation |

## Development

```bash
# Run tests
pytest

# Run only unit tests
pytest tests/unit/

# Lint and format
ruff check src/ tests/
ruff format src/ tests/

# Type check
mypy src/
```

## Project Structure

```
src/poob/
    agent/          # Conversational Discord agent (tool-calling LLM)
    browser/        # Playwright browser management, stealth, GraphQL intercept
    discord_bot/    # Discord bot, cogs, notifier, message formatter
    llm/            # LLM provider implementations (Ollama, Cerebras, Groq, Gemini, OpenRouter, VLM cascade)
    scanner/        # Patrol engine, scheduler, interest matching
    sites/          # Site adapter plugin system (facebook/)
    skills/         # Deal evaluation pipeline (triage, enrichment, VLM, eBay/retail lookup)
    storage/        # SQLite database, models, repositories
    utils/          # Logging, retry utilities
    config.py       # All configuration (pydantic-settings)
    main.py         # Application entry point
```

## License

MIT
