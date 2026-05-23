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
        assert app_config.ollama_model == "qwen3:8b"
        assert app_config.llm_temperature == 0.3
        assert app_config.ollama_num_ctx == 12000
        assert app_config.ollama_json_temperature == 0.0

    def test_cloud_llm_defaults(self, app_config):
        """Cloud LLM defaults to empty key (local-only mode)."""
        assert app_config.google_api_key == ""
        assert app_config.google_model == "gemini-2.5-flash"

    def test_vision_model_default(self, app_config):
        """Vision model defaults to qwen2.5vl:7b."""
        assert app_config.vision_model == "qwen2.5vl:7b"

    def test_browser_defaults(self, app_config):
        """Browser defaults to non-headless with vision enabled."""
        assert app_config.browser_headless is False
        assert app_config.browser_use_vision is True

    def test_scanning_defaults(self, app_config):
        """Scanning defaults to 15-minute intervals."""
        assert app_config.scan_interval_minutes == 15
        assert app_config.scan_max_listings_per_query == 50
        assert app_config.scan_stealth_min_delay_ms == 1500
        assert app_config.scan_stealth_max_delay_ms == 4000

    def test_deal_radar_defaults(self, app_config):
        """Deal radar enabled by default with 'good' minimum score."""
        assert app_config.deal_radar_enabled is True
        assert app_config.deal_radar_min_score == "good"

    def test_storage_defaults(self):
        """Default storage paths are relative to project root."""
        from poob.config import AppConfig

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


class TestPatrolConfig:
    """Verify patrol-related default values."""

    def test_patrol_enabled_by_default(self, app_config):
        assert app_config.patrol_enabled is True

    def test_patrol_categories_default(self, app_config):
        assert "electronics" in app_config.patrol_categories
        assert "furniture" in app_config.patrol_categories
        assert "vehicles" not in app_config.patrol_categories
        assert len(app_config.patrol_categories) == 9

    def test_patrol_radius_defaults(self, app_config):
        assert app_config.patrol_base_radius_miles == 40
        assert app_config.patrol_radius_jitter == 4

    def test_patrol_timing_defaults(self, app_config):
        assert app_config.patrol_peak_interval_seconds == 180
        assert app_config.patrol_moderate_interval_seconds == 600
        assert app_config.patrol_offpeak_interval_seconds == 900
        assert app_config.patrol_dead_interval_seconds == 1800

    def test_patrol_peak_hours_defaults(self, app_config):
        assert app_config.patrol_peak_hours_start == 16
        assert app_config.patrol_peak_hours_end == 21

    def test_patrol_deep_inspect_enabled(self, app_config):
        assert app_config.patrol_deep_inspect_enabled is True

    def test_patrol_days_since_listed(self, app_config):
        assert app_config.patrol_days_since_listed == 1

    def test_patrol_sweep_mode_default(self, app_config):
        assert app_config.patrol_sweep_mode == "unified"

    def test_patrol_radius_oscillation_disabled(self, app_config):
        assert app_config.patrol_radius_oscillation_enabled is False

    def test_stealth_defaults(self, app_config):
        assert app_config.stealth_viewport_randomize is True
        assert app_config.stealth_mouse_movement is True
        assert app_config.stealth_inject_scripts is True

    def test_vision_model_num_ctx_default(self, app_config):
        assert app_config.vision_model_num_ctx == 4096


class TestAppConfigFromEnv:
    """Verify config loads from environment variables."""

    def test_loads_from_env_vars(self, monkeypatch):
        """Config reads from environment variables."""
        from poob.config import AppConfig

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
        from poob.config import AppConfig

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


class TestWakeWordModelPath:
    """The wake-word model path env-var binding.

    Renamed from ``PORCUPINE_KEYWORD_PATH`` to ``WAKE_WORD_MODEL_PATH`` once
    the bot moved off Porcupine onto OpenWakeWord. The new env-var name is
    authoritative; the legacy name keeps working via pydantic AliasChoices
    so existing Komodo stacks don't break on rename. Both can be removed
    once the .env files in the wild have rotated.
    """

    def test_field_default_is_empty(self, monkeypatch, tmp_path):
        # Explicitly clear BOTH env-var names so an OS-level value (e.g.
        # the project's .env loaded via python-dotenv elsewhere in the
        # suite) can't shadow the field's empty default.
        from poob.config import AppConfig

        monkeypatch.delenv("WAKE_WORD_MODEL_PATH", raising=False)
        monkeypatch.delenv("PORCUPINE_KEYWORD_PATH", raising=False)
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
        monkeypatch.setenv("DISCORD_DEALS_CHANNEL_ID", "1")

        config = AppConfig(_env_file=None)
        assert config.wake_word_model_path == ""

    def test_wake_word_model_path_env_sets_field(self, monkeypatch):
        from poob.config import AppConfig

        monkeypatch.delenv("PORCUPINE_KEYWORD_PATH", raising=False)
        monkeypatch.setenv("WAKE_WORD_MODEL_PATH", "/app/hey_poob_v3.onnx")
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
        monkeypatch.setenv("DISCORD_DEALS_CHANNEL_ID", "1")

        config = AppConfig(_env_file=None)
        assert config.wake_word_model_path == "/app/hey_poob_v3.onnx"

    def test_legacy_porcupine_keyword_path_env_still_works(self, monkeypatch):
        from poob.config import AppConfig

        monkeypatch.delenv("WAKE_WORD_MODEL_PATH", raising=False)
        monkeypatch.setenv("PORCUPINE_KEYWORD_PATH", "/app/hey_poob.onnx")
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
        monkeypatch.setenv("DISCORD_DEALS_CHANNEL_ID", "1")

        config = AppConfig(_env_file=None)
        assert config.wake_word_model_path == "/app/hey_poob.onnx"

    def test_new_name_takes_precedence_over_legacy(self, monkeypatch):
        from poob.config import AppConfig

        monkeypatch.setenv("WAKE_WORD_MODEL_PATH", "/app/hey_poob_v3.onnx")
        monkeypatch.setenv("PORCUPINE_KEYWORD_PATH", "/app/hey_poob.onnx")
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
        monkeypatch.setenv("DISCORD_DEALS_CHANNEL_ID", "1")

        config = AppConfig(_env_file=None)
        assert config.wake_word_model_path == "/app/hey_poob_v3.onnx"


class TestAppConfigValidation:
    """Verify config validates input types."""

    def test_invalid_channel_id_type(self):
        """Non-numeric channel ID should fail validation."""
        from poob.config import AppConfig

        with pytest.raises(Exception):
            AppConfig(
                discord_bot_token="test",
                discord_deals_channel_id="not-a-number",
            )

    def test_missing_required_token(self, monkeypatch):
        """Missing bot token should fail."""
        from poob.config import AppConfig

        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("DISCORD_DEALS_CHANNEL_ID", raising=False)

        with pytest.raises(Exception):
            AppConfig(_env_file=None)
