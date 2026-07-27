"""Tests for per-subsystem log routing: the ``component`` field, the JSONL
firehose, the curated interactive stream, and ANSI gating.

The machine-parseable JSONL sink must capture EVERY event with a ``component``
tag (first segment of the logger name) and round-trip through ``json.loads``.
The curated ``voice.log`` interactive stream must include music (the gap the
prior prefix tuple left open). Console output must drop ANSI when stdout is not
a TTY. See docs/decisions/per-subsystem-component-logging.md.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
import structlog

import poob.utils.logging as plog


@pytest.fixture(autouse=True)
def _reset_log_state():
    """Isolate each test: tear down file handlers + reset structlog config."""
    yield
    plog._teardown_file_logs()
    structlog.reset_defaults()


# --- component derivation --------------------------------------------------


@pytest.mark.parametrize(
    "name,comp",
    [
        ("voice.session", "voice"),
        ("voice", "voice"),
        ("scanner.patrol_engine", "scanner"),
        ("brain.poob", "brain"),
        ("music.player", "music"),
        ("music", "music"),
        ("poob.voice.voice_compat", "voice"),  # stdlib package-qualified name
        ("discord.voice_client", "voice"),  # alias: DAVE / WS 4014 are voice
        ("discord.voice_client.gateway", "voice"),
        ("discord.notifier", "discord"),  # non-voice discord stays discord
        ("discord.music_cog", "discord"),
        ("", ""),
    ],
)
def test_component_of(name: str, comp: str) -> None:
    assert plog._component_of(name) == comp


def test_add_component_processor() -> None:
    ed = {"event": "queued", "logger": "music.player", "level": "info"}
    out = plog._add_component(None, "info", ed)
    assert out["component"] == "music"


def test_add_component_noop_without_logger() -> None:
    ed = {"event": "x", "level": "info"}
    out = plog._add_component(None, "info", ed)
    assert "component" not in out


def test_add_component_respects_explicit_binding() -> None:
    # An explicitly bound component wins over the derived one.
    ed = {"event": "x", "logger": "music.player", "component": "custom", "level": "info"}
    out = plog._add_component(None, "info", ed)
    assert out["component"] == "custom"


# --- JSONL sink: lossless, all components, round-trip ----------------------


def _read_jsonl(tmp_path) -> list[dict]:
    plog._jsonl_handler.flush()
    return [
        json.loads(line)
        for line in (tmp_path / "poob.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_jsonl_captures_all_components(tmp_path) -> None:
    plog.setup_logging(log_level="INFO", log_dir=tmp_path)
    plog.get_logger("voice.session").info("wake", ms=306)
    plog.get_logger("scanner.patrol_engine").info("patrol done", deals=0)
    plog.get_logger("music.player").info("queued", title="song")

    objs = _read_jsonl(tmp_path)
    comps = {o["component"] for o in objs}
    # Scanner is NOT filtered out of the JSONL — it is the lossless firehose.
    assert {"voice", "scanner", "music"} <= comps

    music = next(o for o in objs if o["component"] == "music")
    assert music["event"] == "queued"
    assert music["title"] == "song"
    assert music["logger"] == "music.player"
    assert music["level"] == "info"
    # Structlog and stdlib paths share one ISO-UTC format (trailing Z).
    assert music["timestamp"].endswith("Z")


def test_jsonl_all_lines_parse(tmp_path) -> None:
    plog.setup_logging(log_level="INFO", log_dir=tmp_path)
    names = ("voice.session", "scanner.patrol_engine", "music.player", "brain.poob", "llm.groq")
    for name in names:
        plog.get_logger(name).info("evt", k="v")

    for obj in _read_jsonl(tmp_path):  # raises if any line is not valid JSON
        assert {"component", "event", "timestamp", "level"} <= obj.keys()


def test_jsonl_captures_stdlib_voice(tmp_path) -> None:
    # voice_compat (DAVE / WS 4014) uses stdlib logging — must land in the
    # JSONL with component=voice, so a voice audit reading only JSONL sees it.
    plog.setup_file_logs(tmp_path)
    logging.getLogger("poob.voice.voice_compat").warning(
        "[VoiceCompat] Voice websocket closed: code=4014"
    )
    rec = next(o for o in _read_jsonl(tmp_path) if "4014" in o.get("event", ""))
    assert rec["component"] == "voice"
    assert rec["level"] == "warning"
    # Stdlib path timestamp matches the structlog ISO-UTC format (trailing Z).
    assert rec["timestamp"].endswith("Z")


def test_jsonl_sink_noop_when_unconfigured() -> None:
    plog._teardown_file_logs()
    ed = {"event": "x", "logger": "voice.session", "level": "info"}
    assert plog._jsonl_sink_processor(None, "info", ed) is ed


def test_jsonl_renders_exception(tmp_path) -> None:
    plog.setup_logging(log_level="INFO", log_dir=tmp_path)
    try:
        raise ValueError("boom-token")
    except ValueError:
        plog.get_logger("scanner.patrol_engine").exception("blew up")

    rec = next(o for o in _read_jsonl(tmp_path) if o.get("event") == "blew up")
    assert "boom-token" in rec.get("exception", "")
    # The raw exc_info tuple must not leak as a non-serializable field.
    assert "exc_info" not in rec


# --- curated stream: voice.log includes voice + brain + music --------------


def _route(name: str, event: str, **extra) -> None:
    ed = {"event": event, "logger": name, "level": "info", **extra}
    plog._add_component(None, "info", ed)
    plog._curated_tee_processor(None, "info", ed)


def test_voice_log_includes_voice_brain_music_not_scanner(tmp_path) -> None:
    plog.setup_file_logs(tmp_path)
    _route("voice.session", "wake")
    _route("brain.poob", "route")
    _route("music.player", "queued", title="x")
    _route("scanner.patrol_engine", "patrol")
    plog._curated_handlers["voice"].flush()

    text = (tmp_path / "voice.log").read_text(encoding="utf-8")
    assert all(s in text for s in ("wake", "route", "queued"))
    assert "title=x" in text
    assert "patrol" not in text  # scanner stays out of the interactive stream


# --- ANSI gating -----------------------------------------------------------


def test_resolve_colors_true_when_tty() -> None:
    class _Tty:
        def isatty(self) -> bool:
            return True

    assert plog._resolve_colors(_Tty()) is True


def test_resolve_colors_false_when_not_tty() -> None:
    # A StringIO (or any pipe) reports isatty() == False → no ANSI.
    assert plog._resolve_colors(io.StringIO()) is False


def test_resolve_colors_false_for_streamless() -> None:
    assert plog._resolve_colors(object()) is False


def test_console_has_no_ansi_when_not_tty(capsys, monkeypatch) -> None:
    monkeypatch.setattr(plog.sys.stdout, "isatty", lambda: False, raising=False)
    plog.setup_logging(log_level="INFO")
    plog.get_logger("voice.session").info("hello-token")
    out = capsys.readouterr().out
    assert "hello-token" in out
    assert "\x1b[" not in out  # no ANSI escape sequences in a non-TTY stream


def test_console_exception_traceback_has_no_ansi_when_not_tty(capsys, monkeypatch) -> None:
    # ConsoleRenderer's default exception formatter is rich-colored even with
    # colors=False; the plain formatter must keep `docker logs` ANSI-free.
    monkeypatch.setattr(plog.sys.stdout, "isatty", lambda: False, raising=False)
    plog.setup_logging(log_level="INFO")
    try:
        raise ValueError("kaboom")
    except ValueError:
        plog.get_logger("scanner.patrol_engine").exception("blew up")
    out = capsys.readouterr().out
    assert "blew up" in out
    assert "ValueError" in out  # traceback still rendered
    assert "\x1b[" not in out  # but with no ANSI escapes


# --- robustness / regression guards ----------------------------------------


def test_voice_log_renders_exception_traceback(tmp_path) -> None:
    # The interactive stream is the fastest path during a live incident — it must
    # show the actual traceback, not a useless `exc_info=True` token.
    plog.setup_logging(log_level="INFO", log_dir=tmp_path)
    try:
        raise ValueError("boom-curated")
    except ValueError:
        plog.get_logger("voice.session").exception("voice blew up")
    plog._curated_handlers["voice"].flush()

    text = (tmp_path / "voice.log").read_text(encoding="utf-8")
    assert "voice blew up" in text
    assert "boom-curated" in text  # the real traceback
    assert "Traceback" in text
    assert "exc_info=True" not in text  # the noisy boolean is dropped


def test_curated_stream_honors_info_floor(tmp_path) -> None:
    # handle() does not enforce the handler level; a DEBUG event routed through
    # the tee must still be dropped by voice.log's INFO floor.
    plog.setup_file_logs(tmp_path)
    ed = {"event": "debug noise", "logger": "voice.session", "level": "debug"}
    plog._add_component(None, "debug", ed)
    plog._curated_tee_processor(None, "debug", ed)
    plog._curated_handlers["voice"].flush()

    text = (tmp_path / "voice.log").read_text(encoding="utf-8")
    assert "debug noise" not in text


def test_add_component_handles_non_str_logger() -> None:
    # A non-str bound logger must degrade to no-component, never crash the caller.
    ed = {"event": "x", "logger": 123, "level": "info"}
    out = plog._add_component(None, "info", ed)
    assert "component" not in out


def test_jsonl_survives_unserializable_extra(tmp_path) -> None:
    # A tuple-keyed dict extra (not JSON-serializable as keys) must not drop the
    # line's core fields — skipkeys keeps it valid and filterable.
    plog.setup_logging(log_level="INFO", log_dir=tmp_path)
    plog.get_logger("voice.session").info("weird extra", bad={("tuple", "key"): 1})

    rec = next(o for o in _read_jsonl(tmp_path) if o.get("event") == "weird extra")
    assert rec["component"] == "voice"
    assert rec["level"] == "info"
    assert rec["timestamp"].endswith("Z")


def test_no_bare_structlog_loggers_in_src() -> None:
    # Every production logger must be named via get_logger(name); a bare
    # structlog.get_logger() yields component-less, jq-invisible JSONL lines.
    import pathlib
    import re

    poob_root = pathlib.Path(plog.__file__).resolve().parents[1]
    offenders = [
        str(p)
        for p in poob_root.rglob("*.py")
        if re.search(r"structlog\.get_logger\(\s*\)", p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"bare structlog.get_logger() (no component): {offenders}"
