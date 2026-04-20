# Poob - Project Conventions

## CRITICAL: Documentation-Driven Development

**`docs/technical_notes.md` is the single source of truth for how this system works.** Every architectural decision, pitfall, and lesson learned lives there. You MUST:

1. **READ `docs/technical_notes.md` BEFORE making any changes** to understand existing decisions, known pitfalls, and why things are built the way they are. Do not guess — the answer is probably already documented.
2. **READ `docs/technical_notes.md` BEFORE debugging** — the "Common Pitfalls" section exists because these mistakes have already been made and fixed.
3. **UPDATE `docs/technical_notes.md` AFTER making architectural changes, discovering new quirks, or learning something non-obvious.** If you changed how something works or found out something important, document it. Undocumented changes rot.
4. **When in doubt, check the docs first.** If technical_notes.md doesn't cover it, that's a gap — fill it after you figure it out.

This is not optional. Skipping the docs leads to re-introducing bugs that were already fixed, contradicting existing architecture, and wasting time rediscovering things that are already written down.

## CRITICAL: No Bandaid Fixes

Every fix must be **robust, modular, and integrated**. Never stuff edge-case patches into code to "make it work for now." If a fix requires understanding why the root cause exists, do the research first.

- **Use proper DOM readiness detection**, not longer sleep timers
- **Use existing libraries and services** to solve problems instead of hand-rolling fragile solutions
- **Fix at the right architectural layer** — if data is missing, fix the extraction, don't work around it downstream
- **If you're adding `if x is None: default_value` more than once for the same field**, the extraction is broken — fix the extraction
- **If a value flows through 5 stages and gets lost**, trace the data flow end-to-end and fix where it drops — don't add fallbacks at every stage
- **Every fix must be documented** in `docs/technical_notes.md` with the root cause and rationale

Also read `docs/architecture.md` for the full pipeline documentation.

## Overview
Autonomous deal-hunting bot that monitors Facebook Marketplace, evaluates listings through a multi-stage VLM pipeline, and delivers deal alerts via Discord. Uses Playwright for browser automation, a cascade of cloud and local LLMs for evaluation, and SQLite for storage.

## Architecture

**Before changing any architectural component, read `docs/technical_notes.md` for the rationale behind the current design. Before finishing, update it if the architecture changed.**

- **src layout:** All source code under `src/poob/`
- **Protocol-based interfaces** (not ABC) for all extension points (SiteAdapter, LLMProvider)
- **Async-first:** Every I/O operation is async
- **Dependency injection:** Components receive dependencies via constructor
- **Plugin system:** Site adapters auto-discovered from `src/poob/sites/` subdirectories
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

**The `docs/` directory is the knowledge base for this project. Treat it as a living document — read before you code, update when you learn.**

- **`docs/technical_notes.md`**: Architectural decisions, research findings, known pitfalls, and lessons learned. **READ THIS FIRST before any non-trivial change. UPDATE IT when you discover something new or change how something works.** This file is explicitly mutable — overwrite outdated sections, add new ones.
- **`docs/plans/`**: Write implementation plan BEFORE coding each feature
- **`docs/references/`**: Keep reference materials for 3rd-party tools

### Documentation Workflow
1. **Starting a task**: Read `docs/technical_notes.md` for relevant context
2. **During implementation**: If you hit something surprising or make a design decision, note it for later
3. **After implementation**: Update `docs/technical_notes.md` with any new findings, changed behavior, or pitfalls discovered
4. **Debugging**: Check `docs/technical_notes.md` "Common Pitfalls" section FIRST — the bug you're looking at may already be documented with a fix

## Adding a New Site Adapter
1. Create `src/poob/sites/<site_name>/` directory
2. Implement `adapter.py` with a class satisfying `SiteAdapter` protocol
3. Implement `prompts.py` with LLM task prompts for navigation
4. Implement `parser.py` for listing extraction from agent output
5. Export `Adapter = YourAdapter` in `__init__.py`
6. The `SiteRegistry` will auto-discover it on startup
7. **Update `docs/technical_notes.md`** with any site-specific quirks, rate limits, or extraction gotchas

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
- `python -m poob.main` to start
- `pytest` to run tests

## Reminder: Read and Update the Docs
If you've read this far and are about to start working: go read `docs/technical_notes.md` now. Seriously. It will save you from repeating mistakes that have already been made and fixed.
