"""Application entry point. Wires all components together and starts the bot."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

from poob.config import AppConfig
from poob.llm.provider import (
    build_cloud_llm_with_fallbacks,
    create_knowledge_provider,
    create_llm_provider,
)
from poob.storage.database import init_database
from poob.utils.logging import get_logger, setup_logging


def _ensure_docker_services() -> None:
    """Start docker-compose services (SearXNG) if Docker is available.

    Blocks until the compose up command exits (max 30s). Tries both
    `docker compose` (v2 plugin) and `docker-compose` (legacy v1).
    Logs outcome so failures are visible instead of silently swallowed.
    """
    import importlib
    _log = importlib.import_module("poob.utils.logging").get_logger("main.docker")

    compose_file = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    if not compose_file.exists():
        _log.debug("docker-compose.yml not found, skipping SearXNG start")
        return

    # Prefer docker compose v2 plugin, fall back to standalone docker-compose
    docker = shutil.which("docker")
    docker_compose_standalone = shutil.which("docker-compose")

    def _run_compose(cmd_prefix: list[str]) -> bool:
        """Try check + start with the given compose command prefix. Returns True on success."""
        try:
            check = subprocess.run(
                cmd_prefix + ["-f", str(compose_file), "ps", "-q", "searxng"],
                capture_output=True, text=True, timeout=5,
            )
            if check.stdout.strip():
                _log.info("SearXNG already running")
                return True

            _log.info("Starting SearXNG via docker compose")
            result = subprocess.run(
                cmd_prefix + ["-f", str(compose_file), "up", "-d"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                _log.info("SearXNG started successfully")
                return True
            _log.warning(
                "docker compose up failed",
                returncode=result.returncode,
                stderr=result.stderr.strip()[:200],
            )
            return False
        except Exception as exc:
            _log.debug("docker compose attempt failed", error=str(exc)[:100])
            return False

    if docker and _run_compose([docker, "compose"]):
        return
    if docker_compose_standalone and _run_compose([docker_compose_standalone]):
        return

    _log.warning("Could not start SearXNG — Docker not available or compose failed. MSRP lookups will use text-only fallback.")


async def startup() -> None:
    """Initialize all components and start the application."""
    from poob.browser.manager import BrowserManager
    from poob.discord_bot.bot import ScraperBot
    from poob.discord_bot.notifier import DealNotifier
    from poob.scanner.interest_matcher import InterestMatcher
    from poob.scanner.patrol_engine import PatrolEngine
    from poob.scanner.patrol_scheduler import PatrolScheduler
    from poob.sites.registry import SiteRegistry
    from poob.storage.repositories.deal_repo import DealRepository
    from poob.storage.repositories.listing_repo import ListingRepository
    from poob.storage.repositories.scan_log_repo import ScanLogRepository
    from poob.storage.repositories.exclusion_repo import ExclusionRepository
    from poob.storage.repositories.feedback_repo import FeedbackRepository
    from poob.storage.repositories.watchlist_repo import WatchlistRepository

    from poob.agent.runner import AgentRunner
    from poob.storage.repositories.conversation_repo import ConversationRepository
    from poob.storage.repositories.preferences_repo import UserPreferencesRepository

    config = AppConfig()

    setup_logging(log_level=config.log_level, log_dir=config.log_dir)

    # Suppress noisy library spam
    import logging as _logging
    _logging.getLogger("dave").setLevel(_logging.CRITICAL)  # dave.py C++ logs are very noisy

    log = get_logger("main")

    log.info("Starting Poob", version="0.1.0")

    # Auto-start Docker services (SearXNG) if Docker is available
    _ensure_docker_services()

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
    # Per-guild named playlists (music save/load/list/delete actions).
    # See docs/decisions/music-named-playlists.md.
    from poob.storage.repositories.playlist_repo import GuildPlaylistsRepository
    playlist_repo = GuildPlaylistsRepository(conn)
    # Spotify public-playlist URL resolver. Construction is cheap; the
    # spotipy client is lazy. is_configured() gates the brain action so
    # an unconfigured stack soft-errors instead of crashing. See
    # docs/plans/music-spotify-playlist-import.md.
    from poob.music.spotify import SpotifyPlaylistResolver
    spotify_resolver = SpotifyPlaylistResolver(
        client_id=config.spotify_client_id,
        client_secret=config.spotify_client_secret,
    )
    # Synced-lyrics resolver. Auth-free (LRCLIB); the only config knob
    # would be enable/disable, which the soft-error path covers. See
    # docs/decisions/music-synced-lyrics.md.
    from poob.music.lyrics import LyricsResolver
    lyrics_resolver = LyricsResolver()

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
    from poob.llm.ollama_provider import OllamaProvider

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

    # Create anonymous browser for depersonalized DOM sweeps (no login, clean profile)
    anonymous_browser = None
    if getattr(config, "patrol_anonymous_browser_enabled", True):
        anonymous_browser = BrowserManager(
            headless=True,
            profiles_dir=config.browser_profiles_dir / "anonymous",
            use_vision=False,
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
        from poob.cache.hash_cache import ResultCache
        from poob.quota.persistent_limiter import PersistentKV, build_quotas
        from poob.resilience.circuit_breaker import breaker_registry
        from poob.skills.ebay_lookup import EbayLookupTool
        from poob.skills.orchestrator import SmartDealRadar
        from poob.skills.search_throttle import SearchRateLimiter
        from poob.skills.text_triage import TextTriageService
        from poob.skills.visual_enrichment import VisualEnrichmentService
        from poob.skills.vlm_evaluator import VLMDealEvaluator
        from poob.skills.web_search import (
            GoogleCSESearchProvider,
            MojeekSearchProvider,
            SearchProviderCascade,
            SearXNGSearchProvider,
            SerperSearchProvider,
            TavilySearchProvider,
        )
        from poob.llm.vlm_cascade import build_vlm_cascade

        # --- Phase 1: Persistent quotas + KV state (survive restarts) ---
        quotas = build_quotas(
            db_path=Path(config.quota_db_path),
            vision_limit=config.google_cloud_vision_monthly_quota,
            serpapi_limit=config.serpapi_monthly_quota,
        )
        kv_store = PersistentKV(db_path=Path(config.quota_db_path))
        log.info("Persistent quotas initialized", path=config.quota_db_path)

        # --- Phase 1: Perceptual hash cache (deduplicate API calls) ---
        result_cache = ResultCache(
            cache_dir=Path(config.cache_dir),
            size_limit=config.cache_size_limit,
        )
        log.info("Result cache initialized", path=config.cache_dir)

        # --- Phase 1: Circuit breakers (fast-fail on broken services) ---
        vision_breaker = breaker_registry.register(
            "google_cloud_vision", fail_max=3, recovery_timeout=120.0
        )
        serpapi_breaker = breaker_registry.register(
            "serpapi", fail_max=3, recovery_timeout=120.0
        )
        breaker_registry.register("tavily", fail_max=5, recovery_timeout=60.0)
        breaker_registry.register("serper", fail_max=5, recovery_timeout=60.0)
        breaker_registry.register("searxng", fail_max=3, recovery_timeout=30.0)
        log.info("Circuit breakers registered", services=list(breaker_registry.get_all_states()))

        search_limiter = SearchRateLimiter(min_delay=1.2)

        # Web search cascade: Google CSE → Mojeek → Tavily → Serper → SearXNG → empty
        search_providers: list = []
        search_names: list[str] = []
        # Google CSE: 100/day, highest quality results
        if config.google_cse_api_key and config.google_cse_cx:
            search_providers.append(GoogleCSESearchProvider(
                api_key=config.google_cse_api_key,
                cx=config.google_cse_cx,
                rate_limiter=search_limiter,
                max_results=5,
            ))
            search_names.append("google_cse")
        # Mojeek: 2,000/day, independent index
        if config.mojeek_api_key:
            search_providers.append(MojeekSearchProvider(
                api_key=config.mojeek_api_key,
                rate_limiter=search_limiter,
                max_results=5,
            ))
            search_names.append("mojeek")
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
        # SearXNG: self-hosted unlimited free search (always added as fallback)
        search_providers.append(SearXNGSearchProvider(
            base_url=config.searxng_base_url,
            rate_limiter=search_limiter,
            max_results=5,
        ))
        search_names.append("searxng")
        search_breakers = {
            "tavily": breaker_registry.get("tavily"),
            "serper": breaker_registry.get("serper"),
            "searxng": breaker_registry.get("searxng"),
        }
        search_provider = SearchProviderCascade(
            search_providers,
            result_cache=result_cache,
            breakers={k: v for k, v in search_breakers.items() if v},
        )
        log.info("Search provider cascade built", providers=search_names)

        # Stage 1: Text triage (uses existing cloud LLM chain)
        text_triage = TextTriageService(
            llm=cloud_llm,
            batch_size=config.triage_batch_size,
        )
        log.info("Text triage initialized", batch_size=config.triage_batch_size)

        # Stage 2: Visual enrichment with persistent quotas, cache, and breakers
        visual_enrichment = VisualEnrichmentService(
            config,
            vision_quota=quotas["google_cloud_vision"],
            serpapi_quota=quotas["serpapi"],
            result_cache=result_cache,
            vision_breaker=vision_breaker,
            serpapi_breaker=serpapi_breaker,
            kv_store=kv_store,
        )
        log.info(
            "Visual enrichment initialized (with persistent quotas + cache)",
            vision_api=bool(config.google_cloud_vision_api_key),
            serpapi=bool(config.serpapi_api_key),
        )

        # Stage 3: VLM cascade with hybrid voting
        vlm_cascade = build_vlm_cascade(config, kv_store=kv_store)
        vlm_evaluator = VLMDealEvaluator(vlm_cascade, config, result_cache=result_cache)
        log.info("VLM evaluator initialized (voting=%s)", config.vlm_voting_enabled)

        # eBay lookup (now fed enriched queries from visual enrichment)
        ebay_tool = EbayLookupTool(search_provider=search_provider, result_cache=result_cache)

        # Retail/MSRP lookup (uses search + LLM to estimate retail prices)
        from poob.skills.retail_lookup import RetailLookupTool

        retail_tool = RetailLookupTool(
            llm=cloud_llm,
            search_provider=search_provider,
        )
        log.info("Retail lookup initialized")

        smart_deal_radar = SmartDealRadar(
            text_triage=text_triage,
            visual_enrichment=visual_enrichment,
            vlm_evaluator=vlm_evaluator,
            ebay_lookup=ebay_tool,
            retail_lookup=retail_tool,
            search_cascade=search_provider,
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
        anonymous_browser=anonymous_browser,
        config=config,
    )

    # Build patrol scheduler (adaptive timing based on time of day)
    scheduler = PatrolScheduler(engine, config=config)

    # --- Conversational Agent ---
    prefs_repo = UserPreferencesRepository(conn)
    conversation_repo = ConversationRepository(conn)

    # Agent brain LLM: fast tool-calling models for responsive Discord chat.
    # Chain: Groq GPT-OSS 120B → Groq Llama 70B → NVIDIA NIM → Gemini 3 Flash → Ollama.
    # GPT-OSS 120B is reasoning-capable at 500 T/sec on Groq hardware.
    # Gemini 3 Flash replaces 2.5 Flash Lite (which was a slow 10-50s thinking model).
    agent_brain_models = []
    agent_brain_names = []

    # Primary: Groq GPT-OSS 120B — reasoning-capable, sub-second inference
    if config.groq_api_key and config.agent_groq_model:
        from langchain_groq import ChatGroq as AgentChatGroq

        agent_brain_models.append(AgentChatGroq(
            model=config.agent_groq_model,
            api_key=config.groq_api_key,
            temperature=config.llm_temperature,
            max_retries=0,
        ))
        agent_brain_names.append(f"groq:{config.agent_groq_model}")

    # Fallback 1: Groq Llama 3.3 70B — proven tool-calling, fast
    if config.groq_api_key:
        from langchain_groq import ChatGroq as AgentChatGroq

        agent_brain_models.append(AgentChatGroq(
            model=config.groq_model,
            api_key=config.groq_api_key,
            temperature=config.llm_temperature,
            max_retries=0,
        ))
        agent_brain_names.append(f"groq:{config.groq_model}")

    # Fallback 2: NVIDIA NIM — fast, good tool calling
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

    # Fallback 3: Gemini 3 Flash — fast non-thinking model (replaces slow 2.5 Flash Lite)
    if config.google_api_key:
        from langchain_google_genai import ChatGoogleGenerativeAI

        agent_brain_models.append(ChatGoogleGenerativeAI(
            model=config.agent_google_model_fast,
            google_api_key=config.google_api_key,
            temperature=config.llm_temperature,
            max_retries=0,
        ))
        agent_brain_names.append(f"gemini:{config.agent_google_model_fast}")

    # Failsafe: Local Ollama — always available, free, unlimited
    agent_brain_models.append(langchain_llm_provider.chat_model)
    agent_brain_names.append(f"ollama:{langchain_llm_provider.model_name}")

    if len(agent_brain_models) > 1:
        agent_brain_llm = agent_brain_models[0].with_fallbacks(agent_brain_models[1:])
    else:
        agent_brain_llm = agent_brain_models[0]
    log.info("Agent brain fallback chain", providers=agent_brain_names)

    # Deal sub-agent: personality=False because PoobBrain handles personality.
    # AgentRunner now operates purely as a functional deal tool-calling agent.
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
        personality=False,
    )
    log.info("AgentRunner initialized (sub-agent mode, no personality)")

    # --- Unified PoobBrain ---
    # One brain everywhere: text channels, DMs, voice calls.
    # Casual conversation uses a fast LLM (Groq). Deal-related requests are
    # routed to the AgentRunner sub-agent via native function calling.
    from poob.brain.poob import PoobBrain

    # Use the proven tool-calling model (70B) for routing, NOT the voice 8B.
    # 8B models produce malformed function calls on short inputs like "start scan".
    # Cerebras model comes from the auto-detected cloud LLM (not the hardcoded default).
    poob_brain = PoobBrain(
        deal_agent=agent_runner,
        groq_api_key=config.groq_api_key,
        groq_model=config.groq_model,  # 70b for text + function calling (voice streaming uses 8b separately)
        cerebras_api_key=config.cerebras_api_key,
        cerebras_model=config.cerebras_model,
        nvidia_api_key=config.nvidia_api_key,
        nvidia_model=config.agent_nvidia_model,
        ollama_base_url=config.ollama_base_url,
        ollama_model=config.ollama_model,
        max_history=config.voice_conversation_max_history,
        max_tokens=300,
        max_tokens_voice=config.voice_llm_max_tokens,
    )
    log.info("PoobBrain initialized (unified personality layer)")

    # --- Voice Chat ---
    voice_session_factory = None
    if config.voice_enabled:
        # Ensure opus encoder is loaded — discord.py bundles libopus but may not
        # auto-load it when invoked via `python -m`. Without opus, all voice
        # output is silent (PCM frames can't be encoded to Opus for Discord).
        import discord.opus
        if not discord.opus.is_loaded():
            discord.opus._load_default()
            if not discord.opus.is_loaded():
                log.error("libopus not loaded — voice output will be SILENT")
            else:
                log.info("libopus loaded manually", loaded=True)

        from poob.voice.audio_buffer import VADConfig
        from poob.voice.fillers import FillerPlayer, generate_fillers
        from poob.voice.session import VoiceSession
        from poob.voice.stt import build_stt_cascade
        from poob.voice.tts import build_tts_cascade
        from poob.voice.voice_compat import apply_voice_compat_patches

        # Apply Pycord DAVE E2EE patches (production-tested approach from
        # GabrielAgrela/Discord-Brain-Rot). Patches identify, received_message,
        # poll_event, unpack_audio, and _get_voice_packet for full DAVE support.
        apply_voice_compat_patches()

        stt_providers = build_stt_cascade(
            groq_api_key=config.groq_api_key,
            deepgram_api_key=config.deepgram_api_key,
            google_api_key=config.google_api_key,
            preferred="deepgram",  # Purpose-built STT, faster than Whisper, no hallucination
        )
        tts_providers = build_tts_cascade(
            preferred=config.voice_tts_provider,
            voice=config.voice_tts_voice,
            rate=config.voice_tts_rate,
            google_api_key=config.google_cloud_vision_api_key,
            google_voice=config.voice_google_tts_voice,
            google_speaking_rate=config.voice_google_tts_rate,
        )
        vad_config = VADConfig(
            energy_threshold=config.voice_energy_threshold,
            silence_duration_ms=config.voice_silence_threshold_ms,
            min_speech_duration_ms=config.voice_min_speech_ms,
            max_speech_duration_ms=config.voice_max_utterance_seconds * 1000,
        )

        # Generate filler audio clips for latency masking
        filler_paths = await generate_fillers(voice=config.voice_tts_voice)
        filler_player = FillerPlayer(filler_paths)

        # Dual pipeline config (OpenWakeWord + Deepgram streaming).
        # wake_word_model_path is the env-driven path to the .onnx model
        # (see docs/gotchas/wake-word-model-path-conventions.md).
        dual_pipeline_config = {
            "wake_word_model_path": config.wake_word_model_path,
            "deepgram_api_key": config.deepgram_api_key,
            "deepgram_model": config.deepgram_model,
        }

        def _make_voice_session(vc: object) -> VoiceSession:
            session = VoiceSession(
                voice_client=vc,  # type: ignore[arg-type]
                stt_providers=stt_providers,  # type: ignore[arg-type]
                tts_providers=tts_providers,  # type: ignore[arg-type]
                brain=poob_brain,
                vad_config=vad_config,
                dual_pipeline_config=dual_pipeline_config,
            )
            session.filler_player = filler_player
            return session

        voice_session_factory = _make_voice_session

        use_dual = bool(config.deepgram_api_key)  # Only needs Deepgram — wake word is local
        log.info(
            "Voice chat initialized",
            stt=[p.name for p in stt_providers],
            tts=[p.name for p in tts_providers],
            brain="PoobBrain",
            dual_pipeline=use_dual,
        )

    # Build Discord bot
    bot = ScraperBot(config)
    bot.patrol_engine = engine
    bot.patrol_scheduler = scheduler
    bot.notifier = notifier
    bot.site_registry = registry
    bot.watchlist_repo = watchlist_repo
    bot.deal_repo = deal_repo
    bot.listing_repo = listing_repo
    bot.poob_brain = poob_brain
    bot.feedback_repo = feedback_repo
    bot.scan_log_repo = scan_log_repo
    bot.playlist_repo = playlist_repo
    bot.spotify_resolver = spotify_resolver
    bot.lyrics_resolver = lyrics_resolver
    bot.voice_session_factory = voice_session_factory

    log.info("Startup complete. Launching bot and scheduler.")

    # Start browsers with timeout — don't let browser deadlock block Discord.
    # The main authenticated browser loads a real user_data_dir with days of
    # extensions + cookies and genuinely takes 45-60s to cold-start on homelab.
    # 90s is our outer ceiling; browser-use's `bubus` event bus has its own
    # hardcoded 30s-per-handler timeout that often fires first and propagates
    # up as asyncio.TimeoutError. If that happens, the log warning fires but
    # the browser may still finish starting in the background — patrol
    # cycles will pick it up via get_page() on the next iteration.
    try:
        await asyncio.wait_for(
            browser_manager.start(cookies_file="facebook/cookies.json"),
            timeout=90.0,
        )
        log.info("Main browser startup complete within outer timeout")
    except asyncio.TimeoutError:
        log.warning(
            "Browser startup hit a timeout — continuing; session may still "
            "finish starting in the background (bubus internal timeout is "
            "30s, our outer is 90s)",
        )
    except Exception as exc:
        log.warning("Browser startup failed", error=str(exc)[:100])

    if anonymous_browser:
        try:
            await asyncio.wait_for(anonymous_browser.start(), timeout=30.0)
            log.info("Anonymous browser started (headless, no login)")
        except (asyncio.TimeoutError, Exception) as exc:
            log.warning("Anonymous browser failed/timed out", error=str(exc)[:100])
            anonymous_browser = None

    # Auto-start the patrol scheduler loop unless explicitly disabled.
    # scheduler.start() creates a background asyncio task and returns
    # immediately; the bot event loop below owns the lifetime.
    if getattr(config, "patrol_scheduler_auto_start", True):
        await scheduler.start()
        log.info("Patrol scheduler auto-started")
    else:
        log.info("Patrol scheduler NOT auto-started (patrol_scheduler_auto_start=False)")

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(bot.start(config.discord_bot_token))
    finally:
        await browser_manager.stop()
        if anonymous_browser:
            try:
                await anonymous_browser.stop()
            except Exception:
                pass
        await conn.close()


def main() -> None:
    """Synchronous entry point."""
    asyncio.run(startup())


if __name__ == "__main__":
    main()
