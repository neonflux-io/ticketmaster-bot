"""Structured logging via rich + file handler."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

_console = Console()
_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

# Strip `[bold red]...[/bold red]` style Rich markup before writing to disk.
_RICH_TAG_RE = re.compile(r"\[/?[a-zA-Z0-9_ #,.()]+\]")


class _StripMarkupFilter(logging.Filter):
    """Remove Rich markup from a record's rendered message."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Render once with args applied so we can strip cleanly.
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        cleaned = _RICH_TAG_RE.sub("", message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        return True


def setup_logger(
    name: str = "ticketmaster-bot",
    level: str = "INFO",
    log_file: str | None = "logs/bot.log",
) -> logging.Logger:
    """Configure and return a logger with rich console + optional file output."""
    level_upper = (level or "INFO").upper()
    if level_upper not in _VALID_LEVELS:
        raise ValueError(
            f"Unknown log level {level!r}. Must be one of {sorted(_VALID_LEVELS)}"
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
