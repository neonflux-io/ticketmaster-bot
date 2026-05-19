"""Tests for src/utils/logger.py - markup stripping + level validation."""
from __future__ import annotations

import pytest

from src.utils.logger import setup_logger


def test_setup_logger_invalid_level():
    with pytest.raises(ValueError, match="Unknown log level"):
        setup_logger(level="VERBOSE")


def test_setup_logger_strips_markup_for_file(tmp_path):
    log_file = tmp_path / "bot.log"
    logger = setup_logger(level="DEBUG", log_file=str(log_file))
    logger.warning("[bold red]boom[/bold red]")
    # Flush all handlers
    for h in logger.handlers:
        h.flush()
    text = log_file.read_text()
    assert "boom" in text
    assert "[bold red]" not in text
    assert "[/bold red]" not in text
