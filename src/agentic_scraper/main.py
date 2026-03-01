"""Application entry point. Wires all components together and starts the bot."""

from __future__ import annotations

import asyncio

from agentic_scraper.config import AppConfig
from agentic_scraper.llm.provider import create_knowledge_provider, create_llm_provider
from agentic_scraper.storage.database import init_database
from agentic_scraper.utils.logging import get_logger, setup_logging


async def startup() -> None:
    """Initialize all components and start the application."""
    from agentic_scraper.browser.manager import BrowserManager
    from agentic_scraper.discord_bot.bot import ScraperBot
    from agentic_scraper.discord_bot.notifier import DealNotifier
    from agentic_scraper.scanner.interest_matcher import InterestMatcher
    from agentic_scraper.scanner.patrol_engine import PatrolEngine
    from agentic_scraper.scanner.patrol_scheduler import PatrolScheduler
    from agentic_scraper.sites.registry import SiteRegistry
    from agentic_scraper.storage.repositories.deal_repo import DealRepository
    from agentic_scraper.storage.repositories.listing_repo import ListingRepository
    from agentic_scraper.storage.repositories.scan_log_repo import ScanLogRepository
    from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

    from agentic_scraper.agent.runner import AgentRunner
    from agentic_scraper.storage.repositories.conversation_repo import ConversationRepository
    from agentic_scraper.storage.repositories.preferences_repo import UserPreferencesRepository

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
    # 1. Browser LLM: browser-use's own ChatOllama (required by browser-use 0.12+)
    log.info("Initializing browser LLM", provider=config.llm_provider)
    langchain_llm_provider = create_llm_provider(config)
    if langchain_llm_provider.is_available():
        log.info("Browser LLM available", model=langchain_llm_provider.model_name)
    else:
        log.warning(
            "Browser LLM not reachable - scanning will fail until it's available",
            model=langchain_llm_provider.model_name,
            url=config.ollama_base_url,
        )

    # browser-use 0.12+ requires its own LLM wrapper, not LangChain's
    if config.browser_llm_provider == "nvidia" and config.nvidia_api_key:
        from browser_use import ChatOpenAI as BrowserUseChatOpenAI

        browser_llm = BrowserUseChatOpenAI(
            model=config.nvidia_model,
            api_key=config.nvidia_api_key,
            base_url=config.nvidia_base_url,
            max_completion_tokens=8192,
        )
        log.info("Browser LLM using NVIDIA NIM", model=config.nvidia_model)
    elif config.browser_llm_provider == "gemini" and config.google_api_key:
        from browser_use import ChatGoogle as BrowserUseChatGoogle

        browser_llm = BrowserUseChatGoogle(
            model=config.browser_google_model,
            api_key=config.google_api_key,
        )
        log.info("Browser LLM using Gemini", model=config.browser_google_model)
    else:
        from browser_use import ChatOllama as BrowserUseChatOllama

        browser_llm = BrowserUseChatOllama(
            model=config.ollama_model,
            host=config.ollama_base_url,
        )
        log.info("Browser LLM using Ollama", model=config.ollama_model)

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

    # 3. Knowledge LLM: world-knowledge tasks (retail price, category estimation)
    # Cerebras free tier (235B model, 1M tokens/day) > Gemini > Ollama fallback
    knowledge_llm_provider = create_knowledge_provider(config)
    if knowledge_llm_provider and knowledge_llm_provider.is_available():
        knowledge_llm = knowledge_llm_provider.chat_model
        log.info("Knowledge LLM initialized", model=knowledge_llm_provider.model_name)
    else:
        knowledge_llm = json_llm  # fallback to local Ollama
        if knowledge_llm_provider:
            log.warning(
                "Knowledge LLM not reachable, falling back to local Ollama",
                model=knowledge_llm_provider.model_name,
            )
        else:
            log.info("No cloud API key configured, using local Ollama for knowledge tasks")

    # 4. Vision LLM: image-based identification (with configurable context window)
    vision_llm = json_llm  # fallback if no vision model configured
    if config.vision_model:
        from langchain_ollama import ChatOllama

        vision_llm = ChatOllama(
            base_url=config.ollama_base_url,
            model=config.vision_model,
            temperature=config.ollama_json_temperature,
            format="json",
            num_ctx=config.vision_model_num_ctx,
        )
        log.info(
            "Vision LLM initialized",
            model=config.vision_model,
            num_ctx=config.vision_model_num_ctx,
        )

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

    # Configure Facebook adapter: scan mode, credentials, extraction LLM
    fb_adapter = registry.get("facebook_marketplace")
    if fb_adapter:
        fb_adapter.set_max_listings(config.scan_max_listings_per_query)
        fb_adapter.set_scan_mode(config.scan_mode)
        fb_adapter.set_json_llm(json_llm)
        if config.facebook_email:
            fb_adapter.set_credentials(config.facebook_email, config.facebook_password)
            log.info("Facebook credentials injected into adapter")
        log.info("Facebook adapter configured", scan_mode=config.scan_mode)

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
        visual_tool = VisualIdentifyTool(vision_llm, max_images=config.vision_max_images)
        ebay_tool = EbayLookupTool(timeout_seconds=config.ebay_http_timeout_seconds)
        retail_tool = RetailLookupTool(knowledge_llm)
        category_tool = CategoryEstimateTool(knowledge_llm)

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
            ebay_marketplace_deflator=config.ebay_marketplace_deflator,
        )
        log.info(
            "SmartDealRadar v2 initialized",
            deflator=config.ebay_marketplace_deflator,
            max_images=config.vision_max_images,
        )

    # Build patrol engine (replaces ScanEngine)
    engine = PatrolEngine(
        browser_manager=browser_manager,
        listing_repo=listing_repo,
        watchlist_repo=watchlist_repo,
        deal_repo=deal_repo,
        scan_log_repo=scan_log_repo,
        notifier=notifier,
        interest_matcher=InterestMatcher(),
        smart_deal_radar=smart_deal_radar,
        config=config,
    )

    # Build patrol scheduler (adaptive timing replaces fixed interval)
    scheduler = PatrolScheduler(engine, config=config)

    # --- Conversational Agent ---
    prefs_repo = UserPreferencesRepository(conn)
    conversation_repo = ConversationRepository(conn)

    # Agent brain LLM: default Ollama (free forever, unlimited).
    # Override via AGENT_LLM_PROVIDER env var to "nvidia" or "gemini" if desired.
    if config.agent_llm_provider == "nvidia" and config.nvidia_api_key:
        from langchain_openai import ChatOpenAI as LangChainChatOpenAI

        agent_brain_llm = LangChainChatOpenAI(
            model=config.agent_nvidia_model,
            api_key=config.nvidia_api_key,
            base_url=config.nvidia_base_url,
            temperature=config.llm_temperature,
        )
        log.info("Agent brain using NVIDIA NIM", model=config.agent_nvidia_model)
    elif config.agent_llm_provider == "gemini" and config.google_api_key:
        from langchain_google_genai import ChatGoogleGenerativeAI

        agent_brain_llm = ChatGoogleGenerativeAI(
            model=config.agent_google_model,
            google_api_key=config.google_api_key,
            temperature=config.llm_temperature,
        )
        log.info("Agent brain using Gemini", model=config.agent_google_model)
    else:
        agent_brain_llm = langchain_llm_provider.chat_model
        log.info("Agent brain using local LLM", model=langchain_llm_provider.model_name)

    agent_runner = AgentRunner(
        llm=agent_brain_llm,
        prefs_repo=prefs_repo,
        watchlist_repo=watchlist_repo,
        conversation_repo=conversation_repo,
        deal_repo=deal_repo,
        listing_repo=listing_repo,
        scheduler=scheduler,
        max_iterations=config.agent_max_tool_iterations,
        scan_log_repo=scan_log_repo,
    )
    log.info("AgentRunner initialized")

    # Build Discord bot
    bot = ScraperBot(config)
    bot.patrol_engine = engine
    bot.patrol_scheduler = scheduler
    bot.notifier = notifier
    bot.site_registry = registry
    bot.watchlist_repo = watchlist_repo
    bot.deal_repo = deal_repo
    bot.listing_repo = listing_repo
    bot.agent_runner = agent_runner

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
