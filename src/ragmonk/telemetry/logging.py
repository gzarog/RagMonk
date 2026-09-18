"""Structured logging: JSON records to a file, Rich rendering to a TTY.

File contents are never logged -- only paths, ids, and durations -- so
log files stay safe to attach to a bug report.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.logging import RichHandler

_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys())


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "component": getattr(record, "component", record.name),
            "event": getattr(record, "event", record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(
    *, logs_dir: Path, level: str = "info", console_format: str = "text"
) -> logging.Logger:
    logger = logging.getLogger("ragmonk")
    logger.setLevel(level.upper())
    logger.handlers.clear()
    logger.propagate = False

    logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(logs_dir / "ragmonk.log", encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    logger.addHandler(file_handler)

    # Per-file INFO events are useful in the log file but too noisy to echo
    # to an interactive terminal on every run, so the console only surfaces
    # WARNING and above regardless of the configured file log level.
    if console_format == "json":
        console_handler: logging.Handler = logging.StreamHandler()
        console_handler.setFormatter(JsonFormatter())
    else:
        console_handler = RichHandler(show_path=False, markup=False)
    console_handler.setLevel(logging.WARNING)
    logger.addHandler(console_handler)
    return logger


class _ComponentAdapter(logging.LoggerAdapter[logging.Logger]):
    """The stdlib adapter's default ``process`` replaces ``extra`` instead of
    merging it, which would silently drop every field passed to ``log_event``.
    """

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        base = dict(self.extra or {})
        kwargs["extra"] = {**base, **kwargs.get("extra", {})}
        return msg, kwargs


def get_logger(component: str) -> logging.LoggerAdapter[logging.Logger]:
    return _ComponentAdapter(logging.getLogger("ragmonk"), {"component": component})


def log_event(
    logger: logging.LoggerAdapter[logging.Logger],
    event: str,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    logger.log(level, event, extra={"event": event, **fields})
