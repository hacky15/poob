"""Application entry point. Wires all components together and starts the bot."""

from __future__ import annotations

import asyncio

from agentic_scraper.config import AppConfig
from agentic_scraper.llm.provider import create_cloud_provider, create_llm_provider
from agentic_scraper.storage.database import init_database
from agentic_scraper.utils.logging import get_logger, setup_logging


async def startup() -> None:
    """Initialize all components and start the application."""
    from agentic_scraper.browser.manager import BrowserManager
    from agentic_scraper.discord_bot.bot import ScraperBot
    from agentic_scraper.discord_bot.notifier import DealNotifier
    from agentic_scraper.scanner.engine import ScanEngine
    from agentic_scraper.scanner.scheduler import ScanScheduler
    from agentic_scraper.scanner.watchlist import WatchlistMatcher
    from agentic_scraper.sites.registry import SiteRegistry
    from agentic_scraper.storage.repositories.deal_repo import DealRepository
    from agentic_scraper.storage.repositories.listing_repo import ListingRepository
    from agentic_scraper.storage.repositories.scan_log_repo import ScanLogRepository
    from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

    config = AppConfig()

    setup_logging(log_level=config.log_level, log_dir=config.log_dir)
    log = get_logger("main")

    log.info("Starting Agentic Web Scraper", version="0.1.0")

    # Initialize database
    log.info("Initializing database", path=str(config.database_path))
    conn = await init_database(config.database_path)
    log.info("Database initialized")

    # Initialize repositories
    listing_repo = ListingRepository(conn)
    watchlist_repo = WatchlistRepository(conn)
    deal_repo = DealRepository(conn)
    scan_log_repo = ScanLogRepository(conn)

    # --- Initialize LLM providers ---
    # 1. Browser LLM: free-form text output (NO format="json")
    log.info("Initializing browser LLM", provider=config.llm_provider)
    browser_llm = create_llm_provider(config)
    if browser_llm.is_available():
        log.info("Browser LLM available", model=browser_llm.model_name)
    else:
        log.warning(
            "Browser LLM not reachable - scanning will fail until it's available",
            model=browser_llm.model_name,
            url=config.ollama_base_url,
        )

    # 2. JSON LLM: structured JSON output for skill extraction
    from agentic_scraper.llm.ollama_provider import OllamaProvider

    json_llm_provider = OllamaProvider(
        model=config.ollama_model,
        base_url=config.ollama_base_url,
        temperature=config.ollama_json_temperature,
        format="json",
        num_ctx=config.ollama_num_ctx,
    )
    json_llm = json_llm_provider.chat_model
    log.info("JSON LLM initialized", model=config.ollama_model, format="json")

    # 3. Vision LLM: image-based identification
    vision_llm = json_llm  # fallback if no vision model configured
    if config.vision_model:
        from langchain_ollama import ChatOllama

        vision_llm = ChatOllama(
            base_url=config.ollama_base_url,
            model=config.vision_model,
            temperature=config.ollama_json_temperature,
            format="json",
        )
        log.info("Vision LLM initialized", model=config.vision_model)

    # 4. Cloud LLM: world-knowledge tasks (retail lookup, category estimate)
    # Falls back to json_llm if no Google API key is set
    cloud_provider = create_cloud_provider(config)
    if cloud_provider and cloud_provider.is_available():
        cloud_llm = cloud_provider.chat_model
        log.info("Cloud LLM available", model=cloud_provider.model_name)
    else:
        cloud_llm = json_llm
        if config.google_api_key:
            log.warning("Cloud LLM not reachable, falling back to local")
        else:
            log.info("No Google API key configured, using local LLM for all tasks")

    # Initialize browser manager
    browser_manager = BrowserManager(
        headless=config.browser_headless,
        profiles_dir=config.browser_profiles_dir,
        use_vision=config.browser_use_vision,
        stealth_min_delay_ms=config.scan_stealth_min_delay_ms,
        stealth_max_delay_ms=config.scan_stealth_max_delay_ms,
    )

    # Discover site adapters
    registry = SiteRegistry()
    registry.discover()
    log.info("Site adapters discovered", sites=registry.list_sites())

    # Initialize Discord notifier and bot
    notifier = DealNotifier()

    # Build SmartDealRadar (v2) if enabled
    smart_deal_radar = None
    if config.deal_radar_enabled and config.deal_radar_version == "v2":
        from agentic_scraper.skills.category_estimate import CategoryEstimateTool
        from agentic_scraper.skills.ebay_lookup import EbayLookupTool
        from agentic_scraper.skills.identify import IdentifyItemTool
        from agentic_scraper.skills.orchestrator import SmartDealRadar
        from agentic_scraper.skills.retail_lookup import RetailLookupTool
        from agentic_scraper.skills.visual import VisualIdentifyTool
        from agentic_scraper.storage.models import DealScore

        identify_tool = IdentifyItemTool(json_llm)
        visual_tool = VisualIdentifyTool(vision_llm)
        ebay_tool = EbayLookupTool(timeout_seconds=config.ebay_http_timeout_seconds)
        retail_tool = RetailLookupTool(cloud_llm)
        category_tool = CategoryEstimateTool(cloud_llm)

        min_score = DealScore(config.deal_radar_min_score)

        smart_deal_radar = SmartDealRadar(
            identify_tool=identify_tool.run,
            visual_identify_tool=visual_tool.run,
            ebay_lookup_tool=ebay_tool.run,
            retail_lookup_tool=retail_tool.run,
            category_estimate_tool=category_tool.run,
            min_score=min_score,
            ebay_min_samples=config.ebay_min_samples,
            scam_threshold_pct=config.deal_radar_scam_threshold_pct,
        )
        log.info("SmartDealRadar v2 initialized")

    # Build scan engine
    engine = ScanEngine(
        registry=registry,
        browser_manager=browser_manager,
        llm_provider=browser_llm,
        matcher=WatchlistMatcher(),
        listing_repo=listing_repo,
        watchlist_repo=watchlist_repo,
        deal_repo=deal_repo,
        scan_log_repo=scan_log_repo,
        notifier=notifier,
        smart_deal_radar=smart_deal_radar,
        deal_radar_max_evaluations=config.deal_radar_max_evaluations,
    )

    # Build scheduler
    scheduler = ScanScheduler(engine, interval_minutes=config.scan_interval_minutes)

    # Build Discord bot
    bot = ScraperBot(config)
    bot.scan_engine = engine
    bot.scan_scheduler = scheduler
    bot.notifier = notifier
    bot.site_registry = registry
    bot.watchlist_repo = watchlist_repo

    log.info("Startup complete. Launching bot and scheduler.")

    # Start browser, then run bot + scheduler concurrently
    await browser_manager.start(cookies_file="facebook/cookies.json")

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(bot.start(config.discord_bot_token))
            tg.create_task(scheduler.start())
    finally:
        await browser_manager.stop()
        await conn.close()


def main() -> None:
    """Synchronous entry point."""
    asyncio.run(startup())


if __name__ == "__main__":
    main()
