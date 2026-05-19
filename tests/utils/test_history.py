"""Tests for :mod:`src.utils.history`.

Covers the validation contract assertion ``io.history-sqlite-roundtrip``: after
a run whose ``history.path`` is set to a ``tmp_path`` file, the file exists,
``PRAGMA user_version`` returns an integer > 0, and rows inserted into the
runs table are retrievable with matching column values via a real
``aiosqlite`` SELECT in pytest.

Every test opens a real on-disk SQLite database under ``tmp_path``. No mocks,
no fakes, and explicitly no ``":memory:"`` databases — the io-worker skill
requires that DB tests exercise the real file path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import pytest

from src.utils.history import HistoryDB, RunRecord


def _ts(year: int = 2026, month: int = 5, day: int = 19, hour: int = 12) -> str:
    """Build a deterministic ISO 8601 UTC timestamp for fixture rows."""
    return (
        datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


async def test_context_manager_creates_db_file(tmp_path: Path) -> None:
    """Entering the async context creates the SQLite file on disk."""
    db_path = tmp_path / "history.sqlite"
    assert not db_path.exists()
    async with HistoryDB(db_path) as db:
        assert isinstance(db, HistoryDB)
    assert db_path.is_file()
    assert db_path.stat().st_size > 0


async def test_init_sets_user_version_and_creates_runs_table(tmp_path: Path) -> None:
    """``init`` applies the schema and bumps ``PRAGMA user_version`` above 0."""
    db_path = tmp_path / "history.sqlite"
    async with HistoryDB(db_path):
        pass  # __aenter__ calls init()

    # Reopen with a raw aiosqlite connection and confirm the contract.
    async with aiosqlite.connect(db_path) as raw:
        cur = await raw.execute("PRAGMA user_version")
        row = await cur.fetchone()
        await cur.close()
        assert row is not None
        version = int(row[0])
        assert version > 0, f"PRAGMA user_version should be > 0, got {version}"

        cur = await raw.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='runs'")
        table_row = await cur.fetchone()
        await cur.close()
        assert table_row is not None, "runs table should exist after init()"

        cur = await raw.execute("PRAGMA table_info('runs')")
        columns = {col[1] for col in await cur.fetchall()}
        await cur.close()

    # Required columns per the feature description.
    required = {"id", "started_at", "ended_at", "account", "event_url", "outcome", "error"}
    missing = required - columns
    assert not missing, f"runs table missing columns: {missing} (have {columns})"


async def test_init_is_idempotent(tmp_path: Path) -> None:
    """Opening the DB twice does not re-create or corrupt the schema."""
    db_path = tmp_path / "history.sqlite"
    async with HistoryDB(db_path) as db:
        await db.insert_run(
            started_at=_ts(),
            ended_at=_ts(hour=13),
            account="alice",
            event_url="https://example.test/event/1",
            outcome="success",
            error=None,
        )

    # Reopen — init() should be a no-op (no exceptions, data preserved).
    async with HistoryDB(db_path) as db:
        rows = await db.query_runs()
        assert len(rows) == 1
        assert rows[0].account == "alice"


async def test_insert_three_rows_query_roundtrip(tmp_path: Path) -> None:
    """Feature description: insert 3 rows, query, assert roundtrip."""
    db_path = tmp_path / "history.sqlite"
    inserts = [
        {
            "started_at": _ts(hour=10),
            "ended_at": _ts(hour=11),
            "account": "alice",
            "event_url": "https://example.test/event/1",
            "outcome": "success",
            "error": None,
        },
        {
            "started_at": _ts(hour=12),
            "ended_at": _ts(hour=12),
            "account": "bob",
            "event_url": "https://example.test/event/2",
            "outcome": "failure",
            "error": "queue timeout",
        },
        {
            "started_at": _ts(hour=14),
            "ended_at": _ts(hour=15),
            "account": "carol",
            "event_url": "https://example.test/event/3",
            "outcome": "abandoned",
            "error": None,
        },
    ]

    async with HistoryDB(db_path) as db:
        ids = []
        for payload in inserts:
            new_id = await db.insert_run(**payload)
            assert isinstance(new_id, int) and new_id > 0
            ids.append(new_id)

        assert len(set(ids)) == 3, f"insert_run must return distinct ids, got {ids}"

        rows = await db.query_runs()
        assert len(rows) == 3
        assert all(isinstance(r, RunRecord) for r in rows)

        # Newest first ordering (descending by id is the canonical contract).
        ordered_accounts = [r.account for r in rows]
        assert ordered_accounts == ["carol", "bob", "alice"], ordered_accounts

        for row, expected in zip(rows, list(reversed(inserts)), strict=True):
            assert row.started_at == expected["started_at"]
            assert row.ended_at == expected["ended_at"]
            assert row.account == expected["account"]
            assert row.event_url == expected["event_url"]
            assert row.outcome == expected["outcome"]
            assert row.error == expected["error"]


async def test_query_runs_limit(tmp_path: Path) -> None:
    """``query_runs(limit=N)`` returns at most ``N`` newest rows."""
    db_path = tmp_path / "history.sqlite"
    async with HistoryDB(db_path) as db:
        for i in range(5):
            await db.insert_run(
                started_at=_ts(hour=10 + i),
                ended_at=_ts(hour=10 + i),
                account=f"acct{i}",
                event_url=f"https://example.test/event/{i}",
                outcome="success",
                error=None,
            )

        limited = await db.query_runs(limit=2)
        assert len(limited) == 2
        assert [r.account for r in limited] == ["acct4", "acct3"]

        all_rows = await db.query_runs(limit=100)
        assert len(all_rows) == 5


async def test_query_runs_rejects_non_positive_limit(tmp_path: Path) -> None:
    """A zero or negative limit is a programmer error and must raise."""
    db_path = tmp_path / "history.sqlite"
    async with HistoryDB(db_path) as db:
        with pytest.raises(ValueError):
            await db.query_runs(limit=0)
        with pytest.raises(ValueError):
            await db.query_runs(limit=-1)


async def test_insert_run_allows_null_error(tmp_path: Path) -> None:
    """``error`` is optional and stored as NULL when omitted."""
    db_path = tmp_path / "history.sqlite"
    async with HistoryDB(db_path) as db:
        new_id = await db.insert_run(
            started_at=_ts(),
            ended_at=_ts(hour=13),
            account="alice",
            event_url="https://example.test/event/1",
            outcome="success",
        )
        rows = await db.query_runs()
        assert len(rows) == 1
        assert rows[0].id == new_id
        assert rows[0].error is None


async def test_double_init_does_not_lose_data(tmp_path: Path) -> None:
    """Calling ``init()`` again on an open DB preserves existing rows."""
    db_path = tmp_path / "history.sqlite"
    async with HistoryDB(db_path) as db:
        await db.insert_run(
            started_at=_ts(),
            ended_at=_ts(hour=13),
            account="alice",
            event_url="https://example.test/event/1",
            outcome="success",
        )
        await db.init()  # explicit re-init
        rows = await db.query_runs()
        assert len(rows) == 1


async def test_history_db_path_accepts_string(tmp_path: Path) -> None:
    """The constructor accepts ``str`` and ``Path`` alike."""
    db_path = tmp_path / "history.sqlite"
    async with HistoryDB(str(db_path)) as db:
        await db.insert_run(
            started_at=_ts(),
            ended_at=_ts(hour=13),
            account="alice",
            event_url="https://example.test/event/1",
            outcome="success",
        )
    assert db_path.is_file()
