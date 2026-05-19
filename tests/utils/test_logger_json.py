"""Tests for the JSON log file format introduced in F5.4.

Covers validation contract assertion ``io.json-log-mode``: when
``config.logging.format == "json"``, every non-empty line in the configured
log file is parseable by ``json.loads()`` into a dict containing the keys
``timestamp``, ``level``, ``name``, and ``message`` (plus ``exc_info`` for
records carrying exception info).

These tests use the real ``setup_logger`` against ``tmp_path`` log files;
no mocks, no fakes.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from src.utils.logger import JsonFormatter, setup_logger

_ISO_8601_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$")

_REQUIRED_KEYS = {"timestamp", "level", "name", "message"}


def _read_lines(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines() if line.strip()]


def test_json_formatter_emits_required_keys(tmp_path: Path) -> None:
    """A handful of records logged under format=json produce parseable JSON lines."""
    log_file = tmp_path / "bot.log"
    logger = setup_logger(
        name="ticketmaster-bot-json-required",
        level="DEBUG",
        log_file=str(log_file),
        format="json",
    )

    logger.debug("debug-line")
    logger.info("info-line %s", "with-args")
    logger.warning("warn-line")
    logger.error("error-line")

    for handler in logger.handlers:
        handler.flush()

    lines = _read_lines(log_file)
    assert len(lines) == 4, f"expected 4 log lines, got {len(lines)}: {lines!r}"

    for raw in lines:
        record = json.loads(raw)
        assert isinstance(record, dict), f"line did not parse to dict: {raw!r}"
        assert _REQUIRED_KEYS.issubset(record), (
            f"missing required keys in {record!r}; need {_REQUIRED_KEYS}"
        )
        assert _ISO_8601_RE.match(record["timestamp"]), (
            f"timestamp not ISO 8601: {record['timestamp']!r}"
        )
        assert record["level"] in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        assert record["name"] == "ticketmaster-bot-json-required"
        assert isinstance(record["message"], str) and record["message"]


def test_json_formatter_levels_match(tmp_path: Path) -> None:
    """Each log call's level string lines up with the JSON ``level`` field."""
    log_file = tmp_path / "levels.log"
    logger = setup_logger(
        name="ticketmaster-bot-json-levels",
        level="DEBUG",
        log_file=str(log_file),
        format="json",
    )
    logger.info("info")
    logger.warning("warn")
    logger.error("err")
    for handler in logger.handlers:
        handler.flush()

    levels = [json.loads(line)["level"] for line in _read_lines(log_file)]
    assert levels == ["INFO", "WARNING", "ERROR"]


def test_json_formatter_message_args_resolved(tmp_path: Path) -> None:
    """``logger.info("hi %s", "x")`` produces ``message == "hi x"`` (args resolved)."""
    log_file = tmp_path / "args.log"
    logger = setup_logger(
        name="ticketmaster-bot-json-args",
        level="INFO",
        log_file=str(log_file),
        format="json",
    )
    logger.info("hello %s number %d", "world", 42)
    for handler in logger.handlers:
        handler.flush()

    lines = _read_lines(log_file)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["message"] == "hello world number 42"


def test_json_formatter_includes_exc_info(tmp_path: Path) -> None:
    """When a record carries exception info, the JSON line includes an ``exc_info`` key."""
    log_file = tmp_path / "exc.log"
    logger = setup_logger(
        name="ticketmaster-bot-json-exc",
        level="DEBUG",
        log_file=str(log_file),
        format="json",
    )
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("caught failure")

    for handler in logger.handlers:
        handler.flush()

    lines = _read_lines(log_file)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["message"] == "caught failure"
    assert "exc_info" in record, f"exc_info missing in {record!r}"
    assert isinstance(record["exc_info"], str)
    assert "RuntimeError" in record["exc_info"]
    assert "boom" in record["exc_info"]


def test_json_formatter_omits_exc_info_when_absent(tmp_path: Path) -> None:
    """A normal log call must NOT carry an ``exc_info`` key in its JSON payload."""
    log_file = tmp_path / "noexc.log"
    logger = setup_logger(
        name="ticketmaster-bot-json-noexc",
        level="INFO",
        log_file=str(log_file),
        format="json",
    )
    logger.info("clean line")
    for handler in logger.handlers:
        handler.flush()

    lines = _read_lines(log_file)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert "exc_info" not in record


def test_json_formatter_strips_rich_markup(tmp_path: Path) -> None:
    """Rich markup like ``[bold red]...[/bold red]`` must not leak into JSON output."""
    log_file = tmp_path / "markup.log"
    logger = setup_logger(
        name="ticketmaster-bot-json-markup",
        level="INFO",
        log_file=str(log_file),
        format="json",
    )
    logger.warning("[bold red]boom[/bold red]")
    for handler in logger.handlers:
        handler.flush()

    lines = _read_lines(log_file)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert "boom" in record["message"]
    assert "[bold red]" not in record["message"]
    assert "[/bold red]" not in record["message"]


def test_rich_format_does_not_emit_json(tmp_path: Path) -> None:
    """Default (rich) format keeps the human-readable file layout — first line is NOT JSON."""
    log_file = tmp_path / "rich.log"
    logger = setup_logger(
        name="ticketmaster-bot-json-default",
        level="INFO",
        log_file=str(log_file),
    )
    logger.info("plain line")
    for handler in logger.handlers:
        handler.flush()

    lines = _read_lines(log_file)
    assert lines, "default format must still write a log line"
    with pytest.raises(json.JSONDecodeError):
        json.loads(lines[0])


def test_invalid_format_rejected(tmp_path: Path) -> None:
    """Unknown ``format`` values raise ValueError at setup."""
    log_file = tmp_path / "bad.log"
    with pytest.raises(ValueError, match="Unknown log format"):
        setup_logger(
            name="ticketmaster-bot-json-bad-format",
            level="INFO",
            log_file=str(log_file),
            format="xml",
        )


def test_json_formatter_class_directly() -> None:
    """``JsonFormatter().format(record)`` produces a parseable JSON object."""
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="direct.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="direct line",
        args=None,
        exc_info=None,
    )
    payload = json.loads(formatter.format(record))
    assert _REQUIRED_KEYS.issubset(payload)
    assert payload["name"] == "direct.test"
    assert payload["level"] == "INFO"
    assert payload["message"] == "direct line"
    assert _ISO_8601_RE.match(payload["timestamp"])
