# Phase 1: Foundation

## Goal
Project skeleton that runs, loads config, connects to SQLite, and verifies the LLM is reachable.

## Scope

### 1. Project Config
- `pyproject.toml` with all dependencies and tool configurations
- `.env.example` template for secrets
- `.gitignore` covering Python, venv, data, browser profiles, IDE files

### 2. Configuration (config.py)
- `AppConfig` class using `pydantic-settings`
- Loads from `.env` file
- Typed fields for: Discord, LLM, browser, scanning, deal radar, storage, site credentials
- Sensible defaults for all non-secret values

### 3. Data Models (storage/models.py)
- `Listing` - A marketplace listing (title, price, location, images, etc.)
- `WatchItem` - A user's saved search with max price and location
- `Deal` - A listing matched against a watch or flagged by deal radar
- `DealScore` - Enum: unknown, fair, good, great, incredible
- `ScanLog` - Audit log for each scan cycle

### 4. Database (storage/database.py)
- `init_database(path)` - Creates SQLite file, initializes schema
- `init_schema(conn)` - Creates all tables
- Tables: listings, watch_items, deals, scan_logs

### 5. Repositories (storage/repositories/)
- `ListingRepository` - CRUD for listings, exists check by site+external_id
- `WatchlistRepository` - CRUD for watch items, list by user, active filter
- `DealRepository` - CRUD for deals, list recent, mark notified
- `ScanLogRepository` - CRUD for scan logs, list recent

### 6. LLM Provider (llm/)
- `LLMProvider` protocol: chat_model property, model_name, is_available()
- `OllamaProvider` implementation using `ChatOllama`
- `create_llm_provider(config)` factory function

### 7. Logging (utils/logging.py)
- `setup_logging(log_level, log_dir)` using structlog
- Console output + file output
- Structured JSON logging for production

### 8. Entry Point (main.py)
- Async main function
- Loads AppConfig
- Initializes database
- Creates LLM provider and checks availability
- Logs startup info

## Tests (written first)
- `tests/conftest.py` - Shared fixtures (app_config, db_connection, mock_llm, sample data)
- `tests/unit/test_config.py` - Config loading, defaults, validation
- `tests/unit/test_models.py` - Dataclass creation, field defaults, enum values
- `tests/unit/test_repositories.py` - CRUD operations with in-memory SQLite

## Verification
- `pytest tests/unit/` passes
- `python -m agentic_scraper.main` starts and logs: config loaded, DB initialized, Ollama status
