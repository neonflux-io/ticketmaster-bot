"""Async SQLite-backed run history.

The :class:`HistoryDB` is an async context manager around an
``aiosqlite.Connection``. On entry it calls :meth:`init`, which creates the
``runs`` table when missing and bumps ``PRAGMA user_version`` to track schema
migrations. The runner uses this DB to persist one row per attempted
purchase, recording the account, target event URL, outcome, optional error
message, and start/end timestamps.

Schema lives in :data:`_SCHEMA_MIGRATIONS` as an ordered list keyed by the
target ``user_version``. Each migration runs sequentially so old databases
upgrade in place without losing data; new databases skip straight to the
latest version.

This module covers the validation contract assertion
``io.history-sqlite-roundtrip``: ``HistoryDB`` writes a real on-disk SQLite
file, ``PRAGMA user_version`` returns a positive integer after init, and a
row inserted via :meth:`insert_run` is retrievable via :meth:`query_runs`
with matching column values.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

import aiosqlite

#: Current target schema version. Bump when adding a migration below.
_CURRENT_VERSION: int = 1

#: Ordered migrations applied to bring the DB from ``user_version`` ``n-1``
#: to ``n``. Each entry is a list of SQL statements run inside the upgrade
#: transaction. Adding a new entry here automatically extends the upgrade
#: path; do not reorder or rewrite existing entries.
_SCHEMA_MIGRATIONS: dict[int, list[str]] = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at  TEXT    NOT NULL,
            ended_at    TEXT    NOT NULL,
            account     TEXT    NOT NULL,
            event_url   TEXT    NOT NULL,
            outcome     TEXT    NOT NULL,
            error       TEXT
        )
        """,
        # Useful for ``query_runs`` ordering — id desc is implicit, but
        # account/event lookups benefit from an explicit index.
        "CREATE INDEX IF NOT EXISTS idx_runs_account ON runs(account)",
        "CREATE INDEX IF NOT EXISTS idx_runs_event_url ON runs(event_url)",
    ],
}


@dataclass(frozen=True)
class RunRecord:
    """A single row from the ``runs`` table.

    Attributes mirror the column names. ``error`` is ``None`` for successful
    runs.
    """

    id: int
    started_at: str
    ended_at: str
    account: str
    event_url: str
    outcome: str
    error: str | None


class HistoryDB:
    """Async context manager wrapping an aiosqlite connection.

    Usage::

        async with HistoryDB("logs/history.sqlite") as db:
            run_id = await db.insert_run(
                started_at=..., ended_at=..., account="alice",
                event_url="...", outcome="success",
            )
            rows = await db.query_runs(limit=20)

    The underlying file is created on first use. The connection is opened on
    ``__aenter__`` and closed on ``__aexit__``; concurrent use of a single
    ``HistoryDB`` instance from multiple tasks is not supported (aiosqlite
    serialises queries on its single worker thread, which is fine for the
    runner's one-row-per-run usage pattern).
    """

    def __init__(self, path: str | Path) -> None:
        self.path: Path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Context manager protocol
    # ------------------------------------------------------------------

    async def __aenter__(self) -> HistoryDB:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        await self.init()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._conn is not None:
            try:
                await self._conn.close()
            finally:
                self._conn = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError(
                "HistoryDB is not open. Use 'async with HistoryDB(path)' to enter "
                "the async context before calling this method."
            )
        return self._conn

    async def _get_user_version(self) -> int:
        conn = self._require_conn()
        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row is not None else 0

    async def _set_user_version(self, version: int) -> None:
        conn = self._require_conn()
        # ``PRAGMA user_version = N`` does not accept bound parameters, so
        # interpolate the validated integer directly.
        await conn.execute(f"PRAGMA user_version = {int(version)}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Create or upgrade the schema as needed.

        Reads ``PRAGMA user_version``, replays any missing migrations in
        order, and bumps the version to :data:`_CURRENT_VERSION`. Safe to
        call multiple times on the same connection.
        """
        conn = self._require_conn()
        current = await self._get_user_version()
        if current >= _CURRENT_VERSION:
            return

        for target_version in range(current + 1, _CURRENT_VERSION + 1):
            statements = _SCHEMA_MIGRATIONS[target_version]
            for sql in statements:
                await conn.execute(sql)
            await self._set_user_version(target_version)
        await conn.commit()

    async def insert_run(
        self,
        *,
        started_at: str,
        ended_at: str,
        account: str,
        event_url: str,
        outcome: str,
        error: str | None = None,
    ) -> int:
        """Insert one row into ``runs`` and return its auto-generated id."""
        conn = self._require_conn()
        cursor = await conn.execute(
            """
            INSERT INTO runs (started_at, ended_at, account, event_url, outcome, error)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (started_at, ended_at, account, event_url, outcome, error),
        )
        await conn.commit()
        new_id = cursor.lastrowid
        await cursor.close()
        if new_id is None:
            raise RuntimeError("insert_run: SQLite did not return a lastrowid")
        return int(new_id)

    async def query_runs(self, limit: int = 50) -> list[RunRecord]:
        """Return the most recent ``limit`` rows, newest first.

        Raises ``ValueError`` if ``limit`` is not a positive integer.
        """
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError(f"limit must be a positive integer, got {limit!r}")
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT id, started_at, ended_at, account, event_url, outcome, error
            FROM runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            RunRecord(
                id=int(row[0]),
                started_at=str(row[1]),
                ended_at=str(row[2]),
                account=str(row[3]),
                event_url=str(row[4]),
                outcome=str(row[5]),
                error=None if row[6] is None else str(row[6]),
            )
            for row in rows
        ]


__all__ = ["HistoryDB", "RunRecord"]
