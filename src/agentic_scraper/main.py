"""Application entry point. Wires all components together and starts the bot."""

from __future__ import annotations

import asyncio

from agentic_scraper.config import AppConfig
from agentic_scraper.llm.provider import (
    build_cloud_llm_with_fallbacks,
    create_knowledge_provider,
    create_llm_provider,
)
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
    from agentic_scraper.storage.repositories.exclusion_repo import ExclusionRepository
    from agentic_scraper.storage.repositories.feedback_repo import FeedbackRepository
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
    feedback_repo = FeedbackRepository(conn)
    exclusion_repo = ExclusionRepository(conn)

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

    # 3. Cloud LLM with fallback chain: Cerebras → Groq → local Ollama
    # Used for accuracy-critical tasks (identify, retail price extraction).
    cloud_llm = build_cloud_llm_with_fallbacks(config, local_fallback=json_llm)

    # 3b. Local fast LLM for simple tasks (category estimation — saves cloud quota)
    local_fast_llm = OllamaProvider(
        model=config.local_fast_model,
        base_url=config.ollama_base_url,
        temperature=config.ollama_json_temperature,
        format="json",
        num_ctx=config.local_fast_num_ctx,
    ).chat_model
    log.info("Local fast LLM initialized", model=config.local_fast_model)

    # 4. Vision LLM: Groq cloud primary (fast ~2-5s), local Ollama fallback (~30s)
    vision_models = []
    vision_names = []

    if config.groq_api_key and config.groq_vision_model:
        from langchain_groq import ChatGroq as VisionChatGroq

        vision_models.append(VisionChatGroq(
            model=config.groq_vision_model,
            api_key=config.groq_api_key,
            temperature=config.ollama_json_temperature,
            max_tokens=500,
            max_retries=0,
        ))
        vision_names.append(f"groq:{config.groq_vision_model}")

    if config.vision_model:
        from langchain_ollama import ChatOllama

        vision_models.append(ChatOllama(
            base_url=config.ollama_base_url,
            model=config.vision_model,
            temperature=config.ollama_json_temperature,
            format="json",
            num_ctx=config.vision_model_num_ctx,
        ))
        vision_names.append(f"ollama:{config.vision_model}")

    if vision_models:
        vision_llm = vision_models[0]
        if len(vision_models) > 1:
            vision_llm = vision_models[0].with_fallbacks(vision_models[1:])
        log.info("Vision LLM fallback chain", providers=vision_names)
    else:
        vision_llm = json_llm
        log.info("Vision LLM using JSON LLM fallback")

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

    # Build SmartDealRadar
    smart_deal_radar = None
    if config.deal_radar_enabled:
        from agentic_scraper.skills.ebay_lookup import EbayLookupTool
        from agentic_scraper.skills.orchestrator import SmartDealRadar
        from agentic_scraper.skills.search_throttle import SearchRateLimiter
        from agentic_scraper.skills.text_triage import TextTriageService
        from agentic_scraper.skills.visual_enrichment import VisualEnrichmentService
        from agentic_scraper.skills.vlm_evaluator import VLMDealEvaluator
        from agentic_scraper.skills.web_search import (
            SearchProviderCascade,
            SerperSearchProvider,
            TavilySearchProvider,
        )
        from agentic_scraper.llm.vlm_cascade import build_vlm_cascade

        search_limiter = SearchRateLimiter(min_delay=1.2)

        # Web search cascade: Tavily → Serper → empty (graceful degrade)
        search_providers: list[TavilySearchProvider | SerperSearchProvider] = []
        search_names: list[str] = []
        if config.tavily_api_key:
            search_providers.append(TavilySearchProvider(
                api_key=config.tavily_api_key,
                rate_limiter=search_limiter,
                max_results=5,
                timeout_seconds=config.ebay_http_timeout_seconds,
            ))
            search_names.append("tavily")
        if config.serper_api_key:
            search_providers.append(SerperSearchProvider(
                api_key=config.serper_api_key,
                rate_limiter=search_limiter,
                max_results=5,
                timeout_seconds=config.ebay_http_timeout_seconds,
            ))
            search_names.append("serper")
        search_provider = SearchProviderCascade(search_providers)
        if search_names:
            log.info("Search provider cascade built", providers=search_names)
        else:
            log.warning("No search API keys set — eBay/retail lookups will fail")

        # Stage 1: Text triage (uses existing cloud LLM chain)
        text_triage = TextTriageService(
            llm=cloud_llm,
            batch_size=config.triage_batch_size,
        )
        log.info("Text triage initialized", batch_size=config.triage_batch_size)

        # Stage 2: Visual enrichment (Google Cloud Vision / SerpAPI cascade)
        visual_enrichment = VisualEnrichmentService(config)
        log.info(
            "Visual enrichment initialized",
            vision_api=bool(config.google_cloud_vision_api_key),
            serpapi=bool(config.serpapi_api_key),
        )

        # Stage 3: VLM cascade (Gemini Flash → Groq → Gemini Pro → OpenRouter → Ollama)
        vlm_cascade = build_vlm_cascade(config)
        vlm_evaluator = VLMDealEvaluator(vlm_cascade, config)
        log.info("VLM evaluator initialized")

        # eBay lookup (now fed enriched queries from visual enrichment)
        ebay_tool = EbayLookupTool(search_provider=search_provider)

        smart_deal_radar = SmartDealRadar(
            text_triage=text_triage,
            visual_enrichment=visual_enrichment,
            vlm_evaluator=vlm_evaluator,
            ebay_lookup=ebay_tool,
            min_deal_quality=config.deal_radar_min_score,
        )
        log.info(
            "SmartDealRadar initialized",
            min_deal_quality=config.deal_radar_min_score,
            vlm_max_images=config.vlm_max_images,
        )

    # Build patrol engine
    engine = PatrolEngine(
        browser_manager=browser_manager,
        listing_repo=listing_repo,
        watchlist_repo=watchlist_repo,
        deal_repo=deal_repo,
        scan_log_repo=scan_log_repo,
        notifier=notifier,
        interest_matcher=InterestMatcher(),
        smart_deal_radar=smart_deal_radar,
        exclusion_repo=exclusion_repo,
        config=config,
    )

    # Build patrol scheduler (adaptive timing based on time of day)
    scheduler = PatrolScheduler(engine, config=config)

    # --- Conversational Agent ---
    prefs_repo = UserPreferencesRepository(conn)
    conversation_repo = ConversationRepository(conn)

    # Agent brain LLM: fast tool-calling models for responsive Discord chat.
    # Groq primary (<1s latency, excellent tool calling), NVIDIA NIM fallback,
    # Gemini only as emergency (thinking model = 10-50s latency per call).
    # Chain: Groq → NVIDIA NIM → Gemini → local Ollama (always available).
    agent_brain_models = []
    agent_brain_names = []

    # Primary: Groq — sub-second inference, great tool-calling support
    if config.groq_api_key:
        from langchain_groq import ChatGroq as AgentChatGroq

        agent_brain_models.append(AgentChatGroq(
            model=config.groq_model,
            api_key=config.groq_api_key,
            temperature=config.llm_temperature,
            max_retries=0,
        ))
        agent_brain_names.append(f"groq:{config.groq_model}")

    # Fallback 1: NVIDIA NIM — fast, good tool calling
    if config.nvidia_api_key:
        from langchain_openai import ChatOpenAI as LangChainChatOpenAI

        agent_brain_models.append(LangChainChatOpenAI(
            model=config.agent_nvidia_model,
            api_key=config.nvidia_api_key,
            base_url=config.nvidia_base_url,
            temperature=config.llm_temperature,
            max_retries=0,
        ))
        agent_brain_names.append(f"nvidia:{config.agent_nvidia_model}")

    # Fallback 2: Gemini — thinking model, slow (10-50s) but capable
    if config.google_api_key:
        from langchain_google_genai import ChatGoogleGenerativeAI

        agent_brain_models.append(ChatGoogleGenerativeAI(
            model=config.agent_google_model,
            google_api_key=config.google_api_key,
            temperature=config.llm_temperature,
            max_retries=0,
        ))
        agent_brain_names.append(f"gemini:{config.agent_google_model}")

    # Failsafe: Local Ollama — always available, free, unlimited
    agent_brain_models.append(langchain_llm_provider.chat_model)
    agent_brain_names.append(f"ollama:{langchain_llm_provider.model_name}")

    if len(agent_brain_models) > 1:
        agent_brain_llm = agent_brain_models[0].with_fallbacks(agent_brain_models[1:])
    else:
        agent_brain_llm = agent_brain_models[0]
    log.info("Agent brain fallback chain", providers=agent_brain_names)

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
        exclusion_repo=exclusion_repo,
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
    bot.feedback_repo = feedback_repo

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
