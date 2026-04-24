"""Application configuration loaded from environment variables and .env file."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    """All configuration loaded from environment variables and .env file.
    Single source of truth for every tunable parameter."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Discord ---
    discord_bot_token: str
    discord_deals_channel_id: int
    discord_command_prefix: str = "!"
    # Bot DMs this user on startup with version + git SHA. Leave 0 to disable.
    discord_owner_user_id: int = 0
    # Injected at Docker build time via GIT_SHA build-arg; "dev" for local runs.
    git_sha: str = "dev"

    # --- LLM ---
    llm_provider: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    ollama_num_ctx: int = 12000
    ollama_json_temperature: float = 0.0
    llm_temperature: float = 0.3

    # --- Search APIs (cascade: Tavily → Serper → empty) ---
    tavily_api_key: str = ""
    serper_api_key: str = ""  # Serper.dev: 2,500 free searches (one-time, no CC)
    searxng_base_url: str = "http://localhost:8080"  # Self-hosted, unlimited free

    # --- Cloud LLM ---
    google_api_key: str = ""
    google_model: str = "gemini-2.5-flash"
    agent_google_model: str = "gemini-2.5-flash-lite"  # Slow thinking model, emergency only
    # Gemini 3 Flash: non-thinking, fast — preferred agent fallback over 2.5-flash-lite
    agent_google_model_fast: str = "gemini-3-flash"

    # --- Cerebras (free tier: 1K RPM with max_tokens capped, 64K TPM, 1M tokens/day) ---
    cerebras_api_key: str = ""
    cerebras_model: str = "qwen-3-235b-a22b-instruct-2507"

    # --- Groq (free tier: 30 RPM, 14,400 RPD, no credit card) ---
    groq_api_key: str = ""
    deepgram_api_key: str = ""  # Deepgram streaming STT ($200 free credit)
    # Deepgram streaming model. "nova-3" (stable, default) or
    # "flux-general-en" (Oct 2025, ~450ms P50 faster, same keyterm API).
    deepgram_model: str = "nova-3"
    picovoice_access_key: str = ""  # Porcupine wake word engine (free tier)
    porcupine_keyword_path: str = ""  # Path to .ppn wake word model file
    groq_model: str = "openai/gpt-oss-20b"
    groq_vision_model: str = "meta-llama/llama-4-scout-17b-16e-instruct"
    # Agent brain primary: GPT-OSS 120B — reasoning-capable, 500 T/sec on Groq
    agent_groq_model: str = "openai/gpt-oss-120b"

    # --- NVIDIA NIM ---
    nvidia_api_key: str = ""
    # qwen3-next: supports grammar-based structured output required by browser-use
    nvidia_model: str = "qwen/qwen3-next-80b-a3b-instruct"
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    # Agent brain model on NVIDIA NIM (tool calling for conversational agent)
    agent_nvidia_model: str = "qwen/qwen3-next-80b-a3b-instruct"

    # --- Browser ---
    browser_llm_provider: str = "nvidia"  # "nvidia", "gemini", or "ollama"
    browser_google_model: str = "gemini-2.0-flash"  # separate model avoids sharing RPM quota
    browser_headless: bool = False
    browser_profiles_dir: Path = Path("browser_profiles")
    browser_use_vision: bool = True

    # --- Scanning ---
    scan_mode: str = "direct"  # "direct" (CDP) or "agent" (browser-use)
    scan_interval_minutes: int = 15
    scan_max_listings_per_query: int = 50  # Per GraphQL page (FB supports up to ~120)
    scan_stealth_min_delay_ms: int = 1500
    scan_stealth_max_delay_ms: int = 4000
    scan_browse_enabled: bool = True  # Browse latest local listings alongside watches

    # --- Deal Radar ---
    deal_radar_enabled: bool = True
    deal_radar_min_score: str = "good"  # Minimum score to create a Deal
    deal_public_min_score: str = "incredible"  # Public channel: only INCREDIBLE deals
    deal_watchlist_min_score: str = "good"  # Watchlist DMs: GOOD or better
    vision_model: str = "qwen2.5vl:7b"  # Vision-capable model (local Ollama VLM fallback)
    vision_model_num_ctx: int = 4096  # Context window for vision model
    # Local fast model for simple tasks — saves cloud quota
    local_fast_model: str = "qwen3:4b"
    local_fast_num_ctx: int = 4096
    ebay_http_timeout_seconds: int = 10
    deal_radar_max_evaluations: int = 50  # Cap applied before detail enrichment

    # --- Visual Enrichment (reverse image search) ---
    google_cloud_vision_api_key: str = ""  # Google Cloud Vision API key
    google_cloud_vision_enabled: bool = True  # Enable Vision API enrichment
    google_cloud_vision_monthly_quota: int = 1000  # Free tier: 1,000 calls/month
    serpapi_api_key: str = ""  # SerpAPI key for Google Lens products
    serpapi_enabled: bool = True  # Enable SerpAPI Google Lens enrichment
    serpapi_monthly_quota: int = 250  # Free tier: 250 searches/month

    # --- VLM Evaluator ---
    vlm_primary_provider: str = "gemini_flash"  # Primary VLM for deal evaluation
    vlm_fallback_providers: list[str] = [
        "gemma_vlm", "groq_vision", "openrouter_qwen3vl",
        "gemini_pro", "openrouter_nemotron", "ollama_vision",
    ]
    vlm_max_images: int = 2  # Images per evaluation (2 = hero + detail, optimal)
    vlm_confidence_threshold: float = 0.6  # Below this, request second opinion
    vlm_second_opinion_enabled: bool = True  # Enable confidence-based escalation
    vlm_evaluation_timeout: int = 30  # Timeout per VLM call (seconds)
    vlm_voting_enabled: bool = True  # Enable parallel voting (3 diverse providers)
    vlm_voting_panel_size: int = 3  # Number of providers in voting panel
    vlm_value_agreement_threshold: float = 0.15  # Max CV for value agreement (15%)

    # --- Gemini VLM (separate from agent Gemini — different quota) ---
    gemini_flash_model: str = "gemini-3-flash-preview"  # VLM primary (rank 4, score 1274)
    gemini_pro_model: str = "gemini-2.5-pro"  # VLM tiebreaker (no free Gemini 3 Pro)
    gemini_flash_rpd: int = 250  # Daily limit tracking
    gemini_pro_rpd: int = 100  # Daily limit tracking
    # Gemini 3.1 Flash Lite: 15 RPM, 1000 RPD free — high-volume voter (rank 35, score 1188)
    gemini_flash_lite_model: str = "gemini-3.1-flash-lite-preview"
    gemini_flash_lite_rpd: int = 1000
    # Gemma 3 27B: multimodal, 1000 RPD free on Google AI Studio
    gemma_vlm_model: str = "gemma-3-27b-it"
    gemma_vlm_rpd: int = 1000

    # --- Together.ai (free Llama Vision, dynamic rate limiting) ---
    together_api_key: str = ""

    # --- Mistral (Pixtral 12B, 1 RPS free tier) ---
    mistral_api_key: str = ""

    # --- Google Custom Search (100 queries/day free) ---
    google_cse_api_key: str = ""
    google_cse_cx: str = ""  # Custom Search Engine ID

    # --- Mojeek (2,000 queries/day free) ---
    mojeek_api_key: str = ""

    # --- OpenRouter (multi-model aggregator, free tier available) ---
    openrouter_api_key: str = ""
    openrouter_model: str = "mistralai/mistral-small-3.1-24b-instruct:free"  # Rank 64, score 1128, free VLM
    openrouter_model_secondary: str = "nvidia/nemotron-nano-12b-v2-vl:free"  # v2 fallback
    openrouter_enabled: bool = True

    # --- Text Triage (batch pre-filter) ---
    triage_batch_size: int = 5  # Listings per triage prompt
    triage_provider: str = "cerebras"  # Primary: cerebras, fallback: groq, failsafe: ollama

    # --- Deal Notification ---
    notification_max_per_day: int = 10  # Cap notifications per user per day (alert fatigue)
    notification_include_distance: bool = True  # Show distance in notifications

    # --- Enrichment + Comparables ---
    enrichment_tavily_enabled: bool = True  # Use Tavily for comparable sales after enrichment
    enrichment_tavily_only_with_product_name: bool = True  # Only search when enrichment found a name

    # --- Conversational Agent ---
    # Agent brain cascade: Groq GPT-OSS 120B → Groq Llama 70B → NVIDIA NIM → Gemini 3 Flash → Ollama.
    agent_llm_provider: str = "auto"  # Unused; agent always cascades fastest-first
    agent_max_history: int = 20
    agent_max_tool_iterations: int = 10

    # --- Timezone ---
    display_timezone: str = "America/Chicago"  # Central Time — used for peak hours + display

    # --- Storage ---
    database_path: Path = Path("data/scraper.db")
    log_dir: Path = Path("data/logs")
    log_level: str = "INFO"
    quota_db_path: Path = Path("data/quota_state.db")  # Persistent quota tracking
    cache_dir: Path = Path("data/cache")  # Perceptual hash cache directory
    cache_size_limit: int = 1_073_741_824  # Max cache size (1GB default)

    # --- Browser Pool ---
    browser_pool_size: int = 1  # Number of browser contexts (1=single, 3+=pool)
    browser_pool_cooldown_seconds: float = 3600.0  # Ban cooldown per slot (1hr)

    # --- Patrol ---
    patrol_enabled: bool = True
    # Auto-start the patrol scheduler loop on bot startup. When False, patrols
    # only run when the user types "scan" or !scan. "Just listed" detection is
    # incompatible with manual triggering, so this defaults to True.
    patrol_scheduler_auto_start: bool = True
    patrol_sweep_mode: str = "unified"  # "unified" (1 page) or "categories" (10+ pages)
    patrol_categories: list[str] = [
        "electronics", "furniture", "sports", "garden",
        "appliances", "free", "toys", "apparel", "entertainment",
    ]
    patrol_base_radius_miles: int = 40
    patrol_radius_jitter: int = 4
    patrol_radius_oscillation_enabled: bool = False
    patrol_inter_category_delay_min_ms: int = 8000  # Was 15000, reduced for faster watchlist sweeps
    patrol_inter_category_delay_max_ms: int = 12000  # Was 25000, still stealthy
    patrol_inter_listing_delay_min_ms: int = 3000
    patrol_inter_listing_delay_max_ms: int = 7000
    # Polling intervals: research shows FB indexing is 3-15 min (eventual consistency)
    patrol_peak_interval_seconds: int = 180  # 3 min (was 5 min)
    patrol_moderate_interval_seconds: int = 600  # 10 min
    patrol_offpeak_interval_seconds: int = 900  # 15 min
    patrol_dead_interval_seconds: int = 1800  # 30 min
    patrol_peak_hours_start: int = 16
    patrol_peak_hours_end: int = 21
    patrol_include_all_categories: bool = True
    patrol_days_since_listed: int = 1
    # Evaluation cutoff: listings older than this are dropped from the VLM pipeline.
    # Notification cutoffs (below) are what enforce the user-facing "just listed"
    # product requirement — evaluation can still run on older items so the
    # watchlist backlog recovery and measurement/observability work.
    listing_max_age_hours: int = 6
    # Public channel notification cutoff: a deal reaching the public
    # #facebook-marketplace channel must be posted within this many minutes.
    # This is the "just listed AND clearly worth something" product rule.
    public_notification_max_age_minutes: int = 10
    # Watchlist DM cutoff: a watchlist match DMed to the interest owner must
    # be within this many minutes old. Slightly laxer than public because
    # missing a watchlist match hurts the user more than a stale public post.
    watchlist_notification_max_age_minutes: int = 30
    patrol_deep_inspect_enabled: bool = True
    # --- Anonymous GraphQL ---
    patrol_anonymous_graphql_enabled: bool = True  # Try depersonalized GraphQL before browser
    patrol_anonymous_browse_categories: list[str] = [
        "",              # Empty query = general browse (returns few but free)
        "electronics",
        "furniture",
        "appliances",
        "free stuff",
        "sporting goods",
        "toys",
        "tools",
    ]
    patrol_anonymous_browser_enabled: bool = True  # Separate headless browser for DOM sweep
    patrol_graphql_min_delay_seconds: float = 6.0  # Rate limit: min delay between GQL requests (was 12)
    patrol_graphql_max_pages: int = 3  # Max pagination pages per search (3 * 50 = ~150 listings)
    patrol_graphql_watchlist_enabled: bool = True  # Use GraphQL for watchlist searches too
    patrol_graphql_concurrency: int = 2  # Concurrent GraphQL search streams (2-3 safe for anon)
    # --- Watchlist Sweep ---
    patrol_watchlist_sweep_enabled: bool = True  # Search FB for each watchlist item per cycle
    patrol_watchlist_max_items: int = 50  # All items every cycle (was 10)
    marketplace_default_location: str = "madison"  # FB city slug for search URL path
    # Geo-filter center point: auto-resolved from marketplace_default_location
    # if left at 0.0. Set explicitly to override (e.g., for a user in a non-mapped city).
    patrol_center_lat: float = 0.0
    patrol_center_lon: float = 0.0
    # --- Scroll Depth ---
    patrol_scroll_steps_category: int = 20  # DOM scroll steps for category sweep (was 5)
    patrol_scroll_steps_search: int = 30  # DOM scroll steps for keyword search (was 15)
    patrol_scroll_until_stable: bool = True  # Keep scrolling until no new listings load
    patrol_scroll_max_stable_checks: int = 3  # Stop after N scrolls with no new content
    # --- Stealth ---
    stealth_viewport_randomize: bool = True
    stealth_mouse_movement: bool = True
    stealth_inject_scripts: bool = True

    # --- Music ---
    music_enabled: bool = True  # Enable music bot integration
    music_volume: float = 0.5  # Default music volume (0.0-1.0)
    music_idle_timeout: float = 300.0  # Seconds idle before auto-stop (0 = disabled)
    music_ytdl_cookie_file: str = ""  # Path to cookies.txt for age-restricted content
    music_duck_volume: float = 0.25  # Music volume when TTS is active (0.0-1.0)

    # --- Voice Chat ---
    voice_enabled: bool = True  # Enable voice channel integration
    voice_stt_provider: str = "groq_whisper"  # groq_whisper | local_whisper
    voice_tts_provider: str = "google_tts"  # google_tts | edge_tts | kokoro
    voice_tts_voice: str = "en-US-RogerNeural"  # Edge TTS voice name (fallback)
    voice_tts_rate: str = "+35%"  # Speech rate for Edge TTS (fallback, match Fenrir pacing)
    voice_google_tts_voice: str = "en-US-Chirp3-HD-Fenrir"  # Google Chirp3-HD — natural, expressive
    voice_google_tts_rate: float = 1.25  # Faster for natural conversational pacing
    voice_silence_threshold_ms: int = 350  # Silence duration before utterance ends (snappier turn-taking)
    voice_energy_threshold: float = 150.0  # RMS energy threshold for speech detection
    voice_min_speech_ms: int = 200  # Minimum speech duration to avoid spurious triggers
    voice_max_utterance_seconds: int = 30  # Max single utterance length
    voice_llm_model: str = "llama-3.1-8b-instant"  # Fast Groq model for voice
    voice_llm_max_tokens: int = 200  # Allow 2-4 sentences for more natural conversation
    voice_conversation_max_history: int = 10  # Rolling context window

    # --- Site Credentials ---
    facebook_email: str = ""
    facebook_password: str = ""
