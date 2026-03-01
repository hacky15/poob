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

    # --- Cloud LLM ---
    google_api_key: str = ""
    google_model: str = "gemini-2.5-flash"
    agent_google_model: str = "gemini-2.5-flash-lite"

    # --- Cerebras (free tier: 30 RPM, 60K TPM, 1M tokens/day) ---
    cerebras_api_key: str = ""
    cerebras_model: str = "qwen-3-235b-a22b-instruct-2507"

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
    scan_mode: str = "direct"  # "direct" (CDP, fast) or "agent" (browser-use, legacy)
    scan_interval_minutes: int = 15
    scan_max_listings_per_query: int = 20
    scan_stealth_min_delay_ms: int = 1500
    scan_stealth_max_delay_ms: int = 4000
    scan_browse_enabled: bool = True  # Browse latest local listings alongside watches

    # --- Deal Radar ---
    deal_radar_enabled: bool = True
    deal_radar_min_score: str = "good"
    deal_radar_version: str = "v2"  # "v1" (LLM-only) or "v2" (skill-based)
    vision_model: str = "qwen3-vl:8b"  # Vision-capable model for image identification
    vision_model_num_ctx: int = 8000  # Context window for vision model
    vision_max_images: int = 3  # Max images per visual identification call
    ebay_lookup_enabled: bool = True
    ebay_http_timeout_seconds: int = 10
    ebay_min_samples: int = 3
    ebay_marketplace_deflator: float = 0.80  # FB sells 15-25% below eBay (no shipping, cash)
    deal_radar_max_evaluations: int = 20
    deal_radar_scam_threshold_pct: float = 80.0

    # --- Conversational Agent ---
    # Ollama default: free forever, unlimited. Slower than cloud but no quota concerns.
    # NVIDIA NIM (40 RPM) and Gemini (20 RPD) available as overrides.
    agent_llm_provider: str = "ollama"  # "ollama", "nvidia", or "gemini"
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
        "electronics", "furniture", "vehicles", "sports", "garden",
        "appliances", "free", "toys", "apparel", "entertainment",
    ]
    patrol_base_radius_miles: int = 20
    patrol_radius_jitter: int = 4
    patrol_radius_oscillation_enabled: bool = False  # Ineffective per research
    patrol_inter_category_delay_min_ms: int = 15000
    patrol_inter_category_delay_max_ms: int = 25000
    patrol_inter_listing_delay_min_ms: int = 3000
    patrol_inter_listing_delay_max_ms: int = 7000
    # Polling intervals: aligned with FB's 15-30 min listing batching delay
    patrol_peak_interval_seconds: int = 300  # 5 min (was 120s, too aggressive)
    patrol_moderate_interval_seconds: int = 600  # 10 min (was 300s)
    patrol_offpeak_interval_seconds: int = 900  # 15 min (was 600s)
    patrol_dead_interval_seconds: int = 1800  # 30 min (was 900s)
    patrol_peak_hours_start: int = 16
    patrol_peak_hours_end: int = 21
    patrol_include_all_categories: bool = True
    patrol_days_since_listed: int = 1
    patrol_deep_inspect_enabled: bool = True  # Retained for backward compat
    # --- Stealth ---
    stealth_viewport_randomize: bool = True
    stealth_mouse_movement: bool = True
    stealth_inject_scripts: bool = True

    # --- Site Credentials ---
    facebook_email: str = ""
    facebook_password: str = ""
