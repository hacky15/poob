"""build_vlm_cascade must skip a rung whose model isn't actually configured.

2026-09-08: Groq exited the vision-model business entirely (confirmed live —
zero vision-capable models in their catalog). groq_vision_model's default was
changed to "" to reflect that, mirroring the existing
`if config.groq_api_key and config.groq_vision_model:` guard main.py already
used for its own separate vision-model wiring. This test guards the
vlm_cascade.py rung, which had no such guard — previously appended a
groq_vision provider unconditionally whenever groq_api_key was set, model
name included, guaranteeing every real call 404s.
"""

from __future__ import annotations

from pathlib import Path

from poob.config import AppConfig
from poob.llm.vlm_cascade import build_vlm_cascade


def _config(tmp_path: Path, **overrides) -> AppConfig:
    return AppConfig(
        _env_file=None,
        discord_bot_token="test-token",
        discord_deals_channel_id=123,
        database_path=tmp_path / "test.db",
        log_dir=tmp_path / "logs",
        groq_api_key="test-groq-key",
        **overrides,
    )


def test_groq_vision_rung_skipped_when_model_is_empty(tmp_path: Path) -> None:
    config = _config(tmp_path, groq_vision_model="")
    cascade = build_vlm_cascade(config)
    names = [p.name for p in cascade._providers]
    assert "groq_vision" not in names


def test_groq_vision_rung_present_when_model_is_configured(tmp_path: Path) -> None:
    config = _config(tmp_path, groq_vision_model="some-future-groq-vision-model")
    cascade = build_vlm_cascade(config)
    names = [p.name for p in cascade._providers]
    assert "groq_vision" in names
