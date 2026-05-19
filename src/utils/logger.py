"""Structured logging via rich + file handler.

Supports two file-format modes:

* ``rich`` (default) — human-readable lines, Rich markup stripped before
  hitting disk so files stay greppable.
* ``json`` — one JSON object per line with ``timestamp``, ``level``,
  ``name``, and ``message`` keys (plus an ``exc_info`` field when the
  record carries exception info). Selected via ``config.logging.format``.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

_console = Console()
_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_VALID_FORMATS = {"rich", "json"}

# Strip `[bold red]...[/bold red]` style Rich markup before writing to disk.
_RICH_TAG_RE = re.compile(r"\[/?[a-zA-Z0-9_ #,.()]+\]")


def _strip_rich_markup(text: str) -> str:
    return _RICH_TAG_RE.sub("", text)


class _StripMarkupFilter(logging.Filter):
    """Remove Rich markup from a record's rendered message."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Render once with args applied so we can strip cleanly.
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        cleaned = _strip_rich_markup(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        return True


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record.

    The payload always includes ``timestamp`` (UTC, ISO 8601 with trailing
    ``Z``), ``level``, ``name``, and ``message``. When the record carries
    ``exc_info``, a formatted traceback is added under ``exc_info``. Rich
    markup is stripped from the message so JSON logs match the rich-text
    sibling file format.
    """

    def format(self, record: logging.LogRecord) -> str:
        message = _strip_rich_markup(record.getMessage())
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "name": record.name,
            "message": message,
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logger(
    name: str = "ticketmaster-bot",
    level: str = "INFO",
    log_file: str | None = "logs/bot.log",
    format: str = "rich",
) -> logging.Logger:
    """Configure and return a logger with rich console + optional file output.

    ``format`` controls the file handler layout. ``"rich"`` (default) keeps
    the historical human-readable line format. ``"json"`` swaps the file
    handler's formatter for :class:`JsonFormatter` so each line is a JSON
    object containing ``timestamp``, ``level``, ``name``, ``message``, and
    an optional ``exc_info`` field.
    """
    level_upper = (level or "INFO").upper()
    if level_upper not in _VALID_LEVELS:
        raise ValueError(
            f"Unknown log level {level!r}. Must be one of {sorted(_VALID_LEVELS)}"
        )
    format_lower = (format or "rich").lower()
    if format_lower not in _VALID_FORMATS:
        raise ValueError(
            f"Unknown log format {format!r}. Must be one of {sorted(_VALID_FORMATS)}"
        )

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level_upper))
    logger.handlers.clear()

    rich_handler = RichHandler(
        console=_console,
        show_time=True,
        show_path=False,
        markup=True,
        rich_tracebacks=True,
    )
    rich_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(rich_handler)

    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path)
        if format_lower == "json":
            file_handler.setFormatter(JsonFormatter())
        else:
            file_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            file_handler.addFilter(_StripMarkupFilter())
        logger.addHandler(file_handler)

    logger.propagate = False
    return logger


def get_console() -> Console:
    return _console
