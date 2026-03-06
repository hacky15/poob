# Agentic Web Scraper - Project Conventions

## Overview
Autonomous deal-hunting bot that monitors Facebook Marketplace, evaluates listings through a multi-stage VLM pipeline, and delivers deal alerts via Discord. Uses Playwright for browser automation, a cascade of cloud and local LLMs for evaluation, and SQLite for storage.

## Architecture
- **src layout:** All source code under `src/agentic_scraper/`
- **Protocol-based interfaces** (not ABC) for all extension points (SiteAdapter, LLMProvider)
- **Async-first:** Every I/O operation is async
- **Dependency injection:** Components receive dependencies via constructor
- **Plugin system:** Site adapters auto-discovered from `src/agentic_scraper/sites/` subdirectories
- **LLM provider cascade:** Agent brain uses Groq → NVIDIA NIM → Gemini → Ollama (fastest-first)
- **VLM evaluation pipeline:** Text triage → visual enrichment → VLM deep evaluation (Gemini Flash → Groq Vision → Gemini Pro → OpenRouter → Ollama)
- **Dual interaction model:** Patrol engine runs autonomously; Discord bot provides conversational agent with tool-calling

## Code Style
- Python 3.12+ features (type unions with `|`, match statements where appropriate)
- `ruff` for linting and formatting (line-length 100)
- `mypy` strict mode
- Docstrings on all public classes and methods (Google style)
- No wildcard imports

## Testing
- Tests live in `tests/` with `unit/`, `integration/`, `e2e/` subdirectories
- **Write tests BEFORE implementation** (TDD)
- `pytest` + `pytest-asyncio` (auto mode)
- Mock at protocol boundaries, not internal implementation
- In-memory SQLite (`:memory:`) for all database tests
- Shared fixtures in `tests/conftest.py`

## Git Conventions
- Conventional commits: `feat:`, `fix:`, `test:`, `docs:`, `refactor:`
- One logical change per commit
- Never commit `.env`, `browser_profiles/`, or `data/`

## Planning & Documentation
- **docs/plans/**: Write implementation plan BEFORE coding each feature
- **docs/references/**: Keep reference materials for 3rd-party tools
- Plans are written as markdown, named by phase or feature

## Adding a New Site Adapter
1. Create `src/agentic_scraper/sites/<site_name>/` directory
2. Implement `adapter.py` with a class satisfying `SiteAdapter` protocol
3. Implement `prompts.py` with LLM task prompts for navigation
4. Implement `parser.py` for listing extraction from agent output
5. Export `Adapter = YourAdapter` in `__init__.py`
6. The `SiteRegistry` will auto-discover it on startup

## Configuration
- All config via environment variables (12-factor app)
- `pydantic-settings` loads from `.env` file
- No hardcoded values - everything in `AppConfig` class
- Secrets (tokens, passwords) only in `.env`, never in code

## Key Dependencies
- `playwright`: Browser automation (with `browser-use` for agent-mode navigation)
- `discord.py`: Two-way Discord bot with conversational agent
- `aiosqlite`: Async SQLite storage
- `pydantic-settings`: Typed configuration from environment
- `structlog`: Structured logging
- `httpx`: HTTP client (eBay/retail lookups, API calls)
- `langchain-ollama`: Local LLM via Ollama
- `google-genai`: Gemini Flash/Pro for VLM evaluation
- `groq`: Groq Cloud for fast agent responses and vision
- `cerebras-cloud-sdk`: Cerebras for text triage
- `openai` (SDK): NVIDIA NIM and OpenRouter access via OpenAI-compatible API

## Running
- `pip install -e ".[dev]"` to install in dev mode
- `playwright install` to install browser binaries
- Copy `.env.example` to `.env` and fill in values
- `python -m agentic_scraper.main` to start
- `pytest` to run tests
