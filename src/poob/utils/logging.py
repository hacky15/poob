"""Structured logging setup using structlog.

In addition to the console stream (everything, interleaved), a dedicated
persistent voice/chat log is written to ``<log_dir>/voice.log`` — separate
from the marketplace/scanner spam and on the data volume, so it survives
redeploys. It captures BOTH structlog voice/brain events (via a tee
processor) and stdlib voice events (voice_compat DAVE / discord voice WS,
incl. the 4014 close) via an attached handler. See
docs/decisions/persistent-voice-log.md.
"""

from __future__ import annotations

import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

# Map string level names to numeric values
_LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# --- Dedicated voice/chat log ---------------------------------------------
# Logger-name prefixes whose events are teed into the persistent voice log.
_VOICE_LOGGER_PREFIXES = ("voice", "brain", "poob.voice")
# Stdlib logger names attached directly (voice_compat uses stdlib logging;
# discord.voice_client carries the WS 4014 / handshake / reconnect events).
_VOICE_STDLIB_LOGGERS = ("poob.voice", "discord.voice_client")
_voice_handler: RotatingFileHandler | None = None
_voice_lock = threading.Lock()


def _is_voice_logger(name: str) -> bool:
    """True if a logger name belongs to the voice/brain/chat surface."""
    return any(name == p or name.startswith(p + ".") for p in _VOICE_LOGGER_PREFIXES)


def setup_voice_log(log_dir: Path) -> None:
    """Wire the persistent, marketplace-free voice/chat log.

    Idempotent. Safe to call even if file creation fails — logging must
    never break the app, so any error is swallowed (console logging still
    works).
    """
    global _voice_handler
    if _voice_handler is not None:
        return
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "voice.log",
            maxBytes=25 * 1024 * 1024,
            backupCount=4,
            encoding="utf-8",
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s")
        )
        _voice_handler = handler
        # Capture stdlib voice/connection events (voice_compat 4014/DAVE,
        # discord voice WS handshake/reconnect). Propagation stays on, so
        # these also remain in the console/docker stream.
        for name in _VOICE_STDLIB_LOGGERS:
            lg = logging.getLogger(name)
            lg.addHandler(handler)
            if lg.level == logging.NOTSET or lg.level > logging.INFO:
                lg.setLevel(logging.INFO)
    except Exception:
        _voice_handler = None


def _emit_voice_line(name: str, level: str, event_dict: dict) -> None:
    """Render one structlog voice/brain event into the voice log."""
    handler = _voice_handler
    if handler is None:
        return
    try:
        ev = event_dict.get("event", "")
        extras = " ".join(
            f"{k}={v}"
            for k, v in event_dict.items()
            if k not in ("event", "logger", "level", "timestamp")
        )
        msg = f"{ev} {extras}".strip()
        numeric = _LEVEL_MAP.get(str(level).upper(), logging.INFO)
        record = logging.LogRecord(name, numeric, "(structlog)", 0, msg, None, None)
        # handle() acquires the handler's own lock (shared with the stdlib
        # loggers attached above) → rotation-safe across all writers.
        handler.handle(record)
    except Exception:
        pass  # never let logging break the app


def _voice_tee_processor(logger, method_name, event_dict):  # type: ignore[no-untyped-def]
    """structlog processor: tee voice/brain events to the persistent log.

    Returns event_dict unchanged so console rendering is unaffected.
    """
    try:
        name = event_dict.get("logger", "")
        if name and _is_voice_logger(name):
            _emit_voice_line(name, event_dict.get("level", method_name), dict(event_dict))
    except Exception:
        pass
    return event_dict


def setup_logging(log_level: str = "INFO", log_dir: Path | None = None) -> None:
    """Configure structlog for console output and optional file logging."""
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)

    numeric_level = _LEVEL_MAP.get(log_level.upper(), logging.INFO)

    structlog.configure(
        processors=[
            *processors,
            # Tee voice/brain events to the persistent voice log BEFORE the
            # console renderer consumes the event_dict.
            _voice_tee_processor,
            structlog.dev.ConsoleRenderer(colors=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    if log_dir is not None:
        setup_voice_log(log_dir)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a named logger instance.

    Binds ``logger=<name>`` into the event context so the voice-log tee can
    route by logger name (``PrintLoggerFactory`` otherwise discards it), and
    so the console stream shows which subsystem emitted each line.
    """
    return structlog.get_logger(name).bind(logger=name)
