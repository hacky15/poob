"""Tests for the curated ``voice.log`` interactive stream.

``voice.log`` is the human-readable interactive surface (voice + brain +
music), kept under the same filename existing consumers already read. It must
(a) capture voice/brain/music structlog events AND stdlib voice events
(voice_compat / discord voice WS), and (b) exclude marketplace/scanner spam.
See docs/decisions/persistent-voice-log.md and
docs/decisions/per-subsystem-component-logging.md.
"""

from __future__ import annotations

import logging

import pytest

import poob.utils.logging as plog


@pytest.fixture(autouse=True)
def _reset_file_handlers():
    """Isolate each test: detach + close all file handlers, reset globals."""
    yield
    plog._teardown_file_logs()


def _route(name: str, event: str, level: str = "info", **extra) -> None:
    """Push one structlog event through the component + curated-tee processors."""
    ed = {"event": event, "logger": name, "level": level, **extra}
    plog._add_component(None, level, ed)
    plog._curated_tee_processor(None, level, ed)


def test_voice_log_writes_voice_event_not_scanner(tmp_path) -> None:
    plog.setup_file_logs(tmp_path)
    _route("voice.dual_pipeline", "Wake word detected", speech_to_wake_ms=306, user="ben")
    _route("scanner.patrol", "Patrol cycle complete", deals=0)
    plog._curated_handlers["voice"].flush()

    text = (tmp_path / "voice.log").read_text(encoding="utf-8")
    assert "Wake word detected" in text
    assert "speech_to_wake_ms=306" in text
    assert "user=ben" in text
    assert "Patrol cycle complete" not in text


def test_brain_and_music_events_land_in_voice_log(tmp_path) -> None:
    plog.setup_file_logs(tmp_path)
    _route("brain.poob", "Routing decision", tool="music")
    _route("music.autoplay", "Autoplay queued next", track="song")
    plog._curated_handlers["voice"].flush()
    text = (tmp_path / "voice.log").read_text(encoding="utf-8")
    assert "Routing decision" in text
    assert "Autoplay queued next" in text


def test_stdlib_voice_logger_propagates_to_voice_log(tmp_path) -> None:
    plog.setup_file_logs(tmp_path)
    # voice_compat uses stdlib logging under poob.voice.* — captured via
    # propagation to the attached voice.log handler (the 4014/DAVE path).
    logging.getLogger("poob.voice.voice_compat").warning(
        "[VoiceCompat] Voice websocket closed: code=4014"
    )
    plog._curated_handlers["voice"].flush()
    text = (tmp_path / "voice.log").read_text(encoding="utf-8")
    assert "4014" in text
    assert "voice_compat" in text


def test_curated_tee_noop_when_not_configured() -> None:
    # No setup → handlers empty → tee must be a safe no-op, never raise.
    plog._teardown_file_logs()
    ed = {"event": "x", "logger": "voice.session", "level": "info", "component": "voice"}
    out = plog._curated_tee_processor(None, "info", ed)
    assert out is ed  # unchanged, same object


def test_get_logger_binds_name() -> None:
    # The component processor + curated tee route by logger name, which
    # PrintLoggerFactory otherwise drops; get_logger must bind it into context.
    log = plog.get_logger("voice.session")
    ctx = getattr(log, "_context", {})
    assert ctx.get("logger") == "voice.session"
