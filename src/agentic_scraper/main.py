"""Application entry point. Wires all components together and starts the bot."""

from __future__ import annotations

import asyncio

from agentic_scraper.config import AppConfig
from agentic_scraper.llm.provider import create_llm_provider
from agentic_scraper.storage.database import init_database
from agentic_scraper.utils.logging import get_logger, setup_logging


async def startup() -> None:
    """Initialize all components and start the application."""
    config = AppConfig()

    setup_logging(log_level=config.log_level, log_dir=config.log_dir)
    log = get_logger("main")

    log.info("Starting Agentic Web Scraper", version="0.1.0")

    # Initialize database
    log.info("Initializing database", path=str(config.database_path))
    conn = await init_database(config.database_path)
    log.info("Database initialized")

    # Initialize LLM provider
    log.info("Initializing LLM provider", provider=config.llm_provider)
    llm = create_llm_provider(config)
    if llm.is_available():
        log.info("LLM is available", model=llm.model_name)
    else:
        log.warning(
            "LLM is not reachable - scanning will fail until it's available",
            model=llm.model_name,
            url=config.ollama_base_url,
        )

    log.info("Startup complete. All systems initialized.")

    # TODO: Phase 3 will add Discord bot + scheduler startup here
    # await asyncio.gather(bot.start(token), scheduler.run())

    await conn.close()


def main() -> None:
    """Synchronous entry point."""
    asyncio.run(startup())


if __name__ == "__main__":
    main()
