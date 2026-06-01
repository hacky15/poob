"""Tests for the dedicated persistent voice/chat log.

The voice log must (a) capture voice/brain structlog events AND stdlib
voice events (voice_compat / discord voice WS), (b) exclude marketplace/
scanner spam, (c) live on the data volume so it survives redeploys. See
docs/decisions/persistent-voice-log.md.
"""

from __future__ import annotations

import logging

import pytest

import poob.utils.logging as plog


@pytest.fixture(autouse=True)
def _reset_voice_handler():
    """Isolate each test: detach + reset the module-level voice handler."""
    yield
    h = plog._voice_handler
    if h is not None:
        for name in plog._VOICE_STDLIB_LOGGERS:
            logging.getLogger(name).removeHandler(h)
        h.close()
    plog._voice_handler = None


def test_is_voice_logger_classification() -> None:
    assert plog._is_voice_logger("voice.session")
    assert plog._is_voice_logger("voice.dual_pipeline")
    assert plog._is_voice_logger("brain")
    assert plog._is_voice_logger("poob.voice.voice_compat")
    # Not voice — must NOT leak into the voice log.
    assert not plog._is_voice_logger("scanner.patrol")
    assert not plog._is_voice_logger("vlm.cascade")
    assert not plog._is_voice_logger("graphql_client")
    assert not plog._is_voice_logger("voiceover")  # prefix-but-not-segment


def test_tee_writes_voice_event_not_scanner(tmp_path) -> None:
    plog.setup_voice_log(tmp_path)
    # Voice event → written.
    plog._voice_tee_processor(
        None, "info",
        {"event": "Wake word detected", "logger": "voice.dual_pipeline",
         "level": "info", "speech_to_wake_ms": 306, "user": "ben"},
    )
    # Scanner event → NOT written.
    plog._voice_tee_processor(
        None, "info",
        {"event": "Patrol cycle complete", "logger": "scanner.patrol",
         "level": "info", "deals": 0},
    )
    for h in [plog._voice_handler]:
        h.flush()
    text = (tmp_path / "voice.log").read_text(encoding="utf-8")
    assert "Wake word detected" in text
    assert "speech_to_wake_ms=306" in text
    assert "user=ben" in text
    assert "Patrol cycle complete" not in text


def test_stdlib_voice_logger_propagates_to_voice_log(tmp_path) -> None:
    plog.setup_voice_log(tmp_path)
    # voice_compat uses stdlib logging under poob.voice.* — must be captured
    # via propagation to the poob.voice handler (this is the 4014/DAVE path).
    logging.getLogger("poob.voice.voice_compat").warning(
        "[VoiceCompat] Voice websocket closed: code=4014"
    )
    plog._voice_handler.flush()
    text = (tmp_path / "voice.log").read_text(encoding="utf-8")
    assert "4014" in text
    assert "voice_compat" in text


def test_tee_noop_when_not_configured() -> None:
    # No setup → handler is None → tee must be a safe no-op, never raise.
    plog._voice_handler = None
    out = plog._voice_tee_processor(
        None, "info", {"event": "x", "logger": "voice.session"},
    )
    assert out == {"event": "x", "logger": "voice.session"}  # unchanged


def test_get_logger_binds_name() -> None:
    # The tee routes by logger name, which PrintLoggerFactory otherwise drops;
    # get_logger must bind it into context.
    log = plog.get_logger("voice.session")
    # structlog BoundLogger keeps bound context in _context
    ctx = getattr(log, "_context", {})
    assert ctx.get("logger") == "voice.session"
