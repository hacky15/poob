"""Structured logging setup using structlog.

One structlog event stream fans out to three sinks:

* **Console** (stdout) — everything, human-readable. ANSI colors are on only
  when stdout is an interactive TTY (off under ``docker logs`` / pipes), so the
  container stream is clean text.
* **``<log_dir>/poob.jsonl``** — every emitted event as one JSON object per line
  (rotating). ``log_level`` gates the firehose just as it gates the console (INFO
  in prod), but among emitted events nothing is subsystem-filtered or truncated.
  The canonical machine-parseable log: filter losslessly by ``component`` with
  ``jq``. Lives on the data volume, so it survives redeploys.
* **Curated text streams** — a small ``{stream: components}`` router tees a
  human-readable subset per surface. ``<log_dir>/voice.log`` is the interactive
  surface: ``voice`` + ``brain`` + ``music``. It also captures stdlib voice
  events (voice_compat DAVE / discord voice WS, incl. the 4014 close) via an
  attached handler.

Every event carries a top-level ``component`` field (first dotted segment of
the logger name, with the legacy stdlib voice loggers aliased to ``voice``).
That single key is what every downstream filter selects on. See
docs/decisions/per-subsystem-component-logging.md and
docs/decisions/persistent-voice-log.md.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
import threading
import traceback
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING, cast

import structlog

if TYPE_CHECKING:
    from pathlib import Path

# Map string level names to numeric values
_LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# --- Component routing -----------------------------------------------------
# The component is the first dotted segment of the logger name. Legacy stdlib
# voice loggers are namespaced outside the structlog vocabulary (voice_compat
# under ``poob.voice.*``, the discord voice WS under ``discord.voice_client``);
# without an alias a naive first-segment split would bucket them as ``poob`` /
# ``discord`` and a single ``component=="voice"`` filter would miss the DAVE /
# 4014 events. Map them back to ``voice``.
_COMPONENT_ALIASES: dict[str, str] = {
    "poob.voice": "voice",
    "discord.voice_client": "voice",
}

# Curated human-readable streams: ``{stream_name: components}``. Each writes
# ``<log_dir>/<stream_name>.log``. Extend by adding a row (registry pattern),
# e.g. ``"scanner": ("scanner", "sites", "browser")``. The interactive surface
# MUST include ``music`` — see docs/decisions/persistent-voice-log.md.
_CURATED_STREAMS: dict[str, tuple[str, ...]] = {
    "voice": ("voice", "brain", "music"),
}

# Stdlib loggers attached straight to a curated stream's handler — these emit
# via stdlib logging, not structlog, so the tee processor never sees them.
_STREAM_STDLIB_LOGGERS: dict[str, tuple[str, ...]] = {
    "voice": ("poob.voice", "discord.voice_client"),
}

# Keys already represented as columns in the curated text format — excluded
# from the trailing ``k=v`` extras so the rendered line stays clean. exc_info /
# exception are excluded because the traceback is rendered by the stdlib
# Formatter from the record's exc_info, not dumped as a noisy ``exc_info=True``.
_CURATED_RESERVED = ("event", "logger", "level", "timestamp", "component", "exc_info", "exception")

# Rotation caps. Curated text streams are scoped; the JSONL firehose carries
# everything, so it gets a larger budget. All on the data volume.
_CURATED_MAX_BYTES = 25 * 1024 * 1024
_CURATED_BACKUPS = 4
_JSONL_FILENAME = "poob.jsonl"
_JSONL_MAX_BYTES = 50 * 1024 * 1024
_JSONL_BACKUPS = 5

# Module-level sink state. Setup is idempotent and guarded by a lock.
_curated_handlers: dict[str, RotatingFileHandler] = {}
_jsonl_handler: RotatingFileHandler | None = None
_setup_lock = threading.Lock()


def _component_of(name: str) -> str:
    """Return the subsystem component for a logger name.

    The first dotted segment, except for the aliased stdlib voice loggers,
    which resolve to ``voice``.
    """
    if not name:
        return ""
    for prefix, component in _COMPONENT_ALIASES.items():
        if name == prefix or name.startswith(prefix + "."):
            return component
    return name.split(".", 1)[0]


def _add_component(logger, method_name, event_dict):  # type: ignore[no-untyped-def]
    """structlog processor: stamp ``component`` onto every event.

    Derived from the bound ``logger`` name. An explicitly bound ``component``
    wins (``setdefault``). No logger name → no component (don't fabricate one).
    """
    name = event_dict.get("logger", "")
    # Defensive: a non-str bound logger value must degrade to no-component, never
    # crash the emitting call site (this processor has no try/except wrapper).
    if isinstance(name, str) and name:
        event_dict.setdefault("component", _component_of(name))
    return event_dict


def _streams_for(name: str) -> list[str]:
    """Curated stream names whose component set includes this logger's component."""
    component = _component_of(name)
    return [s for s, comps in _CURATED_STREAMS.items() if component in comps]


def _iso_utc(created: float) -> str:
    """Epoch seconds → ISO-8601 UTC with a trailing ``Z``, matching structlog's
    ``TimeStamper(fmt="iso", utc=True)`` so both sink paths share one format.
    """
    return datetime.fromtimestamp(created, tz=UTC).isoformat().replace("+00:00", "Z")


def _format_exc(exc: object) -> str:
    """Render an exception or ``(type, value, tb)`` tuple to a traceback string.

    A raw exc_info tuple is not JSON-serializable and ``default=str`` would drop
    the stack to a useless ``<traceback object>`` repr — incidents need the full
    trace in the JSONL.
    """
    if isinstance(exc, BaseException):
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if isinstance(exc, tuple) and len(exc) == 3:
        return "".join(traceback.format_exception(*exc))
    return str(exc)


class _JsonlFormatter(logging.Formatter):
    """Render a log record as a single JSON line.

    structlog-origin records carry the full event dict on ``record.event_dict``;
    we dump that verbatim. stdlib-origin records (voice_compat / discord voice
    WS) are reshaped into the same schema so the file is uniformly one JSON
    object per line, with ``component`` aliased the same way structlog events are.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, "event_dict", None)
        if payload is None:
            payload = {
                "timestamp": _iso_utc(record.created),
                "level": record.levelname.lower(),
                "component": _component_of(record.name),
                "logger": record.name,
                "event": record.getMessage(),
            }
            if record.exc_info and record.exc_info[0] is not None:
                payload["exception"] = _format_exc(record.exc_info)
        else:
            # Render any structlog exc_info into a traceback string. The sink
            # processor has already resolved True → a live tuple; a tuple with no
            # active exception, or a bare True, carries no trace and is dropped.
            exc = payload.pop("exc_info", None)
            if exc and exc is not True and not (isinstance(exc, tuple) and exc[0] is None):
                payload["exception"] = _format_exc(exc)
        try:
            # skipkeys drops any non-str dict key in an extra (JSON keys must be
            # strings); default=str stringifies non-serializable values. Together
            # they keep the line valid without losing the rest of the payload.
            return json.dumps(payload, default=str, ensure_ascii=False, skipkeys=True)
        except Exception:
            # Last-ditch: a record that won't serialize must not crash logging.
            # Keep the always-available scalar fields so the line stays filterable.
            return json.dumps(
                {
                    "timestamp": payload.get("timestamp"),
                    "level": payload.get("level"),
                    "component": payload.get("component"),
                    "logger": payload.get("logger"),
                    "event": str(payload.get("event", "")),
                },
                default=str,
                ensure_ascii=False,
                skipkeys=True,
            )


def setup_jsonl_log(log_dir: Path) -> None:
    """Wire the rotating all-events JSONL firehose.

    Idempotent. Safe to call even if file creation fails — logging must never
    break the app, so any error is swallowed (console logging still works).
    """
    global _jsonl_handler
    with _setup_lock:
        if _jsonl_handler is not None:
            return
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                log_dir / _JSONL_FILENAME,
                maxBytes=_JSONL_MAX_BYTES,
                backupCount=_JSONL_BACKUPS,
                encoding="utf-8",
            )
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(_JsonlFormatter())
            _jsonl_handler = handler
            # stdlib voice events → JSONL too, so the firehose is lossless
            # (DAVE / 4014 captured as JSON via _JsonlFormatter's stdlib path).
            _attach_stdlib_loggers(handler)
        except Exception:
            _jsonl_handler = None


def setup_curated_logs(log_dir: Path) -> None:
    """Wire the per-surface curated text streams.

    Idempotent. Safe to call even if file creation fails — any error leaves the
    curated handlers empty and logging continues via console + JSONL.
    """
    with _setup_lock:
        if _curated_handlers:
            return
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            fmt = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s %(message)s"
            )
            for stream in _CURATED_STREAMS:
                handler = RotatingFileHandler(
                    log_dir / f"{stream}.log",
                    maxBytes=_CURATED_MAX_BYTES,
                    backupCount=_CURATED_BACKUPS,
                    encoding="utf-8",
                )
                handler.setLevel(logging.INFO)
                handler.setFormatter(fmt)
                _curated_handlers[stream] = handler
            for stream, names in _STREAM_STDLIB_LOGGERS.items():
                existing = _curated_handlers.get(stream)
                if existing is not None:
                    _attach_stdlib_loggers(existing, names)
        except Exception:
            _curated_handlers.clear()


def setup_file_logs(log_dir: Path) -> None:
    """Wire all persistent file sinks: the JSONL firehose + curated streams.

    Convenience wrapper around the two setup functions; idempotent. Used by
    ``setup_logging`` and by tests.
    """
    setup_jsonl_log(log_dir)
    setup_curated_logs(log_dir)


def _teardown_file_logs() -> None:
    """Detach + close every file-sink handler and reset module state.

    Pairs with ``setup_file_logs`` for test isolation. Not used in production —
    the app wires its sinks once at startup and runs.
    """
    global _jsonl_handler
    with _setup_lock:
        handlers: list[logging.Handler] = list(_curated_handlers.values())
        if _jsonl_handler is not None:
            handlers.append(_jsonl_handler)
        stdlib_names = {n for group in _STREAM_STDLIB_LOGGERS.values() for n in group}
        for name in stdlib_names:
            lg = logging.getLogger(name)
            for handler in handlers:
                lg.removeHandler(handler)
        for handler in handlers:
            with contextlib.suppress(Exception):
                handler.close()
        _curated_handlers.clear()
        _jsonl_handler = None


def _attach_stdlib_loggers(
    handler: logging.Handler, names: tuple[str, ...] | None = None
) -> None:
    """Attach ``handler`` to the given stdlib loggers (all voice ones if None).

    Propagation stays on, so these events also remain in the console stream.
    """
    if names is None:
        names = tuple(
            n for group in _STREAM_STDLIB_LOGGERS.values() for n in group
        )
    for name in names:
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        if lg.level == logging.NOTSET or lg.level > logging.INFO:
            lg.setLevel(logging.INFO)


def _emit_curated_line(stream: str, name: str, level: str, event_dict: dict[str, object]) -> None:
    """Render one structlog event into a curated text stream."""
    handler = _curated_handlers.get(stream)
    if handler is None:
        return
    try:
        numeric = _LEVEL_MAP.get(str(level).upper(), logging.INFO)
        # handle() does NOT enforce the handler's level (only Logger.callHandlers
        # does); honor the curated stream's declared floor explicitly so the
        # structlog tee path gates the same way the stdlib path does.
        if numeric < handler.level:
            return
        ev = event_dict.get("event", "")
        extras = " ".join(
            f"{k}={v}" for k, v in event_dict.items() if k not in _CURATED_RESERVED
        )
        msg = f"{ev} {extras}".strip()
        # Render the traceback into the line (mirroring the JSONL/console sinks):
        # resolve a True marker to the live exception, then let the stdlib
        # Formatter append it from the record's exc_info.
        exc = event_dict.get("exc_info")
        if exc is True:
            exc = sys.exc_info()
        exc_tuple = exc if isinstance(exc, tuple) and exc[0] is not None else None
        record = logging.LogRecord(name, numeric, "(structlog)", 0, msg, None, exc_tuple)
        # handle() acquires the handler's own lock (shared with the stdlib
        # loggers attached above) → rotation-safe across all writers.
        handler.handle(record)
    except Exception:
        pass  # never let logging break the app


def _curated_tee_processor(logger, method_name, event_dict):  # type: ignore[no-untyped-def]
    """structlog processor: tee each event to every matching curated stream.

    Returns event_dict unchanged so console rendering is unaffected.
    """
    try:
        name = event_dict.get("logger", "")
        if name:
            streams = _streams_for(name)
            if streams:
                level = event_dict.get("level", method_name)
                snapshot = dict(event_dict)
                for stream in streams:
                    _emit_curated_line(stream, name, level, snapshot)
    except Exception:
        pass
    return event_dict


def _jsonl_sink_processor(logger, method_name, event_dict):  # type: ignore[no-untyped-def]
    """structlog processor: write the full event to the JSONL firehose.

    Returns event_dict unchanged so console rendering is unaffected.
    """
    handler = _jsonl_handler
    if handler is not None:
        try:
            level = event_dict.get("level", method_name)
            numeric = _LEVEL_MAP.get(str(level).upper(), logging.INFO)
            # handle() doesn't enforce the handler level; gate explicitly. The
            # firehose handler sits at DEBUG, so this passes everything the
            # structlog filtering bound logger already emitted.
            if numeric >= handler.level:
                name = event_dict.get("logger", "") or "poob"
                record = logging.LogRecord(name, numeric, "(structlog)", 0, "", None, None)
                snapshot = dict(event_dict)
                if snapshot.get("exc_info") is True:
                    # FilteringBoundLogger.exception() injects exc_info=True into
                    # the event (method_name is "error", so structlog's
                    # set_exc_info is inert here). Resolve to the live
                    # (type, value, tb) now — still synchronously inside the
                    # except block — so the JSONL carries the full trace. The
                    # original event_dict keeps exc_info=True for the console.
                    snapshot["exc_info"] = sys.exc_info()
                record.event_dict = snapshot
                handler.handle(record)
        except Exception:
            pass
    return event_dict


def _resolve_colors(stream: object) -> bool:
    """ANSI colors on only when the console output stream is an interactive TTY.

    Detects the actual ConsoleRenderer output stream (stdout, where
    ``PrintLoggerFactory`` writes) rather than stderr, so the flag and the
    stream stay consistent. Both are non-TTY under ``docker logs`` / pipes, so
    the container stream renders as clean text either way.
    """
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except Exception:
        return False


def setup_logging(log_level: str = "INFO", log_dir: Path | None = None) -> None:
    """Configure structlog: console + JSONL firehose + curated text streams."""
    if log_dir is not None:
        # setup_jsonl_log / setup_curated_logs each create the dir under their
        # own swallow-all guard; no unguarded mkdir here, so a bad log_dir can
        # never abort startup (console logging still comes up).
        setup_file_logs(log_dir)

    numeric_level = _LEVEL_MAP.get(log_level.upper(), logging.INFO)

    use_colors = _resolve_colors(sys.stdout)
    if use_colors:
        console_renderer = structlog.dev.ConsoleRenderer(colors=True)
    else:
        # ConsoleRenderer's default exception formatter is rich-colored even when
        # colors=False; force plain (ANSI-free) tracebacks so `docker logs` stays
        # clean text in the container.
        console_renderer = structlog.dev.ConsoleRenderer(
            colors=False, exception_formatter=structlog.dev.plain_traceback
        )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_component,
            # Machine + curated sinks consume the event dict BEFORE the console
            # renderer turns it into a string (a terminal processor).
            _jsonl_sink_processor,
            _curated_tee_processor,
            console_renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a named logger instance.

    Binds ``logger=<name>`` into the event context so the component processor
    and curated tee can route by logger name (``PrintLoggerFactory`` otherwise
    discards it), and so the console stream shows which subsystem emitted each
    line.
    """
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name).bind(logger=name))
