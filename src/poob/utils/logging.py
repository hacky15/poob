"""Structured logging setup using structlog."""

from __future__ import annotations

import logging
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
            structlog.dev.ConsoleRenderer(colors=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a named logger instance."""
    return structlog.get_logger(name)
