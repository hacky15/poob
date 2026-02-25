"""Tests for AppConfig loading, defaults, and validation."""

from pathlib import Path

import pytest


class TestAppConfigDefaults:
    """Verify default values when only required fields are provided."""

    def test_minimal_config(self, app_config):
        """Config loads with only required fields (token + channel ID)."""
        assert app_config.discord_bot_token == "test-token-123"
        assert app_config.discord_deals_channel_id == 123456789

    def test_llm_defaults(self, app_config):
        """LLM defaults to Ollama with standard settings."""
        assert app_config.llm_provider == "ollama"
        assert app_config.ollama_base_url == "http://localhost:11434"
        assert app_config.ollama_model == "llama3.1:8b"
        assert app_config.llm_temperature == 0.3

    def test_browser_defaults(self, app_config):
        """Browser defaults to non-headless with vision enabled."""
        assert app_config.browser_headless is False
        assert app_config.browser_use_vision is True

    def test_scanning_defaults(self, app_config):
        """Scanning defaults to 15-minute intervals."""
        assert app_config.scan_interval_minutes == 15
        assert app_config.scan_max_listings_per_query == 20
        assert app_config.scan_stealth_min_delay_ms == 1500
        assert app_config.scan_stealth_max_delay_ms == 4000

    def test_deal_radar_defaults(self, app_config):
        """Deal radar enabled by default with 'good' minimum score."""
        assert app_config.deal_radar_enabled is True
        assert app_config.deal_radar_min_score == "good"

    def test_storage_defaults(self):
        """Default storage paths are relative to project root."""
        from agentic_scraper.config import AppConfig

        config = AppConfig(
            discord_bot_token="test",
            discord_deals_channel_id=1,
        )
        assert config.database_path == Path("data/scraper.db")
        assert config.log_dir == Path("data/logs")
        assert config.log_level == "INFO"

    def test_discord_command_prefix_default(self, app_config):
        """Default command prefix is '!'."""
        assert app_config.discord_command_prefix == "!"


class TestAppConfigFromEnv:
    """Verify config loads from environment variables."""

    def test_loads_from_env_vars(self, monkeypatch):
        """Config reads from environment variables."""
        from agentic_scraper.config import AppConfig

        monkeypatch.setenv("DISCORD_BOT_TOKEN", "env-token")
        monkeypatch.setenv("DISCORD_DEALS_CHANNEL_ID", "999")
        monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:32b")
        monkeypatch.setenv("SCAN_INTERVAL_MINUTES", "30")
        monkeypatch.setenv("BROWSER_HEADLESS", "true")

        config = AppConfig(_env_file=None)
        assert config.discord_bot_token == "env-token"
        assert config.discord_deals_channel_id == 999
        assert config.ollama_model == "qwen2.5:32b"
        assert config.scan_interval_minutes == 30
        assert config.browser_headless is True

    def test_env_file_loading(self, tmp_path):
        """Config loads from a .env file."""
        from agentic_scraper.config import AppConfig

        env_file = tmp_path / ".env"
        env_file.write_text(
            "DISCORD_BOT_TOKEN=file-token\n"
            "DISCORD_DEALS_CHANNEL_ID=888\n"
            "LLM_TEMPERATURE=0.7\n"
        )
        config = AppConfig(_env_file=str(env_file))
        assert config.discord_bot_token == "file-token"
        assert config.discord_deals_channel_id == 888
        assert config.llm_temperature == 0.7


class TestAppConfigValidation:
    """Verify config validates input types."""

    def test_invalid_channel_id_type(self):
        """Non-numeric channel ID should fail validation."""
        from agentic_scraper.config import AppConfig

        with pytest.raises(Exception):
            AppConfig(
                discord_bot_token="test",
                discord_deals_channel_id="not-a-number",
            )

    def test_missing_required_token(self, monkeypatch):
        """Missing bot token should fail."""
        from agentic_scraper.config import AppConfig

        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("DISCORD_DEALS_CHANNEL_ID", raising=False)

        with pytest.raises(Exception):
            AppConfig(_env_file=None)
