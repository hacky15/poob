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

    # --- Browser ---
    browser_headless: bool = False
    browser_profiles_dir: Path = Path("browser_profiles")
    browser_use_vision: bool = True

    # --- Scanning ---
    scan_interval_minutes: int = 15
    scan_max_listings_per_query: int = 20
    scan_stealth_min_delay_ms: int = 1500
    scan_stealth_max_delay_ms: int = 4000

    # --- Deal Radar ---
    deal_radar_enabled: bool = True
    deal_radar_min_score: str = "good"
    deal_radar_version: str = "v2"  # "v1" (LLM-only) or "v2" (skill-based)
    vision_model: str = "qwen3-vl:8b"  # Vision-capable model for image identification
    ebay_lookup_enabled: bool = True
    ebay_http_timeout_seconds: int = 10
    ebay_min_samples: int = 3
    deal_radar_max_evaluations: int = 10
    deal_radar_scam_threshold_pct: float = 80.0

    # --- Storage ---
    database_path: Path = Path("data/scraper.db")
    log_dir: Path = Path("data/logs")
    log_level: str = "INFO"

    # --- Site Credentials ---
    facebook_email: str = ""
    facebook_password: str = ""
