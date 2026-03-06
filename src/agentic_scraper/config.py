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

    # --- Cloud LLM ---
    google_api_key: str = ""
    google_model: str = "gemini-2.5-flash"
    agent_google_model: str = "gemini-2.5-flash-lite"

    # --- Cerebras (free tier: 1K RPM with max_tokens capped, 64K TPM, 1M tokens/day) ---
    cerebras_api_key: str = ""
    cerebras_model: str = "qwen-3-235b-a22b-instruct-2507"

    # --- Groq (free tier: 30 RPM, 14,400 RPD, no credit card) ---
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    groq_vision_model: str = "meta-llama/llama-4-scout-17b-16e-instruct"

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
    scan_max_listings_per_query: int = 20
    scan_stealth_min_delay_ms: int = 1500
    scan_stealth_max_delay_ms: int = 4000
    scan_browse_enabled: bool = True  # Browse latest local listings alongside watches

    # --- Deal Radar ---
    deal_radar_enabled: bool = True
    deal_radar_min_score: str = "good"  # Minimum score to create a Deal
    deal_public_min_score: str = "incredible"  # Public channel: only INCREDIBLE deals
    deal_watchlist_min_score: str = "good"  # Watchlist DMs: GOOD or better
    vision_model: str = "qwen2.5vl:3b"  # Vision-capable model (local Ollama VLM fallback)
    vision_model_num_ctx: int = 4096  # Context window for vision model
    # Local fast model for simple tasks — saves cloud quota
    local_fast_model: str = "qwen3:4b"
    local_fast_num_ctx: int = 4096
    ebay_http_timeout_seconds: int = 10
    deal_radar_max_evaluations: int = 30

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
        "groq_vision", "gemini_pro", "openrouter", "ollama_vision",
    ]
    vlm_max_images: int = 2  # Images per evaluation (2 = hero + detail, optimal)
    vlm_confidence_threshold: float = 0.6  # Below this, request second opinion
    vlm_second_opinion_enabled: bool = True  # Enable confidence-based escalation
    vlm_evaluation_timeout: int = 30  # Timeout per VLM call (seconds)

    # --- Gemini VLM (separate from agent Gemini — different quota) ---
    gemini_flash_model: str = "gemini-2.5-flash"  # VLM primary
    gemini_pro_model: str = "gemini-2.5-pro"  # VLM second opinion
    gemini_flash_rpd: int = 250  # Daily limit tracking
    gemini_pro_rpd: int = 100  # Daily limit tracking

    # --- OpenRouter (multi-model aggregator, free tier available) ---
    openrouter_api_key: str = ""
    openrouter_model: str = "meta-llama/llama-4-scout-17b-16e-instruct:free"
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
    # Agent brain uses fastest available provider for responsive Discord chat.
    # Cascade: Groq (<1s) → NVIDIA NIM (1-2s) → Gemini (10-50s, thinking model) → Ollama.
    agent_llm_provider: str = "auto"  # Unused; agent always cascades fastest-first
    agent_max_history: int = 20
    agent_max_tool_iterations: int = 10

    # --- Storage ---
    database_path: Path = Path("data/scraper.db")
    log_dir: Path = Path("data/logs")
    log_level: str = "INFO"

    # --- Patrol ---
    patrol_enabled: bool = True
    patrol_sweep_mode: str = "unified"  # "unified" (1 page) or "categories" (10+ pages)
    patrol_categories: list[str] = [
        "electronics", "furniture", "sports", "garden",
        "appliances", "free", "toys", "apparel", "entertainment",
    ]
    patrol_base_radius_miles: int = 40
    patrol_radius_jitter: int = 4
    patrol_radius_oscillation_enabled: bool = False
    patrol_inter_category_delay_min_ms: int = 15000
    patrol_inter_category_delay_max_ms: int = 25000
    patrol_inter_listing_delay_min_ms: int = 3000
    patrol_inter_listing_delay_max_ms: int = 7000
    # Polling intervals: aligned with FB's 15-30 min listing batching delay
    patrol_peak_interval_seconds: int = 300  # 5 min
    patrol_moderate_interval_seconds: int = 600  # 10 min
    patrol_offpeak_interval_seconds: int = 900  # 15 min
    patrol_dead_interval_seconds: int = 1800  # 30 min
    patrol_peak_hours_start: int = 16
    patrol_peak_hours_end: int = 21
    patrol_include_all_categories: bool = True
    patrol_days_since_listed: int = 1
    listing_max_age_hours: int = 6  # Drop listings older than this from evaluation
    patrol_deep_inspect_enabled: bool = True
    # --- Watchlist Sweep ---
    patrol_watchlist_sweep_enabled: bool = True  # Search FB for each watchlist item per cycle
    patrol_watchlist_max_items: int = 10  # Max watchlist items to search per cycle
    # --- Stealth ---
    stealth_viewport_randomize: bool = True
    stealth_mouse_movement: bool = True
    stealth_inject_scripts: bool = True

    # --- Site Credentials ---
    facebook_email: str = ""
    facebook_password: str = ""
