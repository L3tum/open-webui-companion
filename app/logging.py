"""Structured logging configuration for the companion server.

Uses JSON-formatted logs in production for easy parsing by log aggregators.
Falls back to human-readable format when running in the foreground.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class StructuredFormatter(logging.Formatter):
    """JSON structured log formatter for machine-readable output."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Add exception info if present
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exc"] = self.formatException(record.exc_info)

        # Add extra fields
        if hasattr(record, "extra_data"):
            log_entry["data"] = record.extra_data

        return json.dumps(log_entry, default=str)


class ContextAdapter:
    """Adapter that injects extra context into log records."""

    def __init__(self, logger: logging.Logger, **context: Any):
        self.logger = logger
        self.context = context

    def info(self, msg: str, **extra: Any) -> None:
        self._log(logging.INFO, msg, **extra)

    def warning(self, msg: str, **extra: Any) -> None:
        self._log(logging.WARNING, msg, **extra)

    def error(self, msg: str, **extra: Any) -> None:
        self._log(logging.ERROR, msg, **extra)

    def debug(self, msg: str, **extra: Any) -> None:
        self._log(logging.DEBUG, msg, **extra)

    def exception(self, msg: str, **extra: Any) -> None:
        self._log(logging.ERROR, msg, exc_info=True, **extra)

    def _log(self, level: int, msg: str, exc_info: bool = False, **extra: Any) -> None:
        # Merge context + extra
        merged = {**self.context, **extra}
        record = self.logger.makeRecord(
            self.logger.name, level, "(unknown)", 0, msg, (), None
        )
        record.extra_data = merged  # type: ignore[attr-defined]
        self.logger.handle(record)


def setup_logging(level: str = "info", structured: bool = True) -> None:
    """Configure root logger with structured or human-readable formatting.

    Args:
        level: Logging level (debug, info, warning, error).
        structured: If True, use JSON format. If False, use human-readable.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    if structured:
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
    root.addHandler(handler)


def get_logger(name: str, **context: Any) -> ContextAdapter:
    """Get a logger with optional pre-bound context.

    Example:
        logger = get_logger(__name__, request_id="abc123")
        logger.info("Processing request")  # includes request_id in all logs
    """
    return ContextAdapter(logging.getLogger(name), **context)
