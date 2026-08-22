# SPDX-FileCopyrightText: 2026 Pedro Sordo Martínez <amurlaniakea@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Durable state store (FR-5): server_id -> last desc_hash, fetch status, append-only event log.

Single source of truth: dataclasses are imported from `core.diff`, NOT redefined here.
The contract test (test_state.py) asserts `state.CatalogEntry is diff.CatalogEntry`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import asdict
from enum import StrEnum

from .diff import CatalogEntry, DriftEvent, NewArrivalEvent, RemovalEvent  # single source of truth


class FetchStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    server_id TEXT PRIMARY KEY,
    last_hash TEXT NOT NULL,
    removed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fetch_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


class StateStore:
    def __init__(self, path: str = "drift.sqlite"):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        # Seed fetch status row (id=1) so last_fetch_status always has a value.
        self._conn.execute(
            "INSERT OR IGNORE INTO fetch_status (id, status) VALUES (1, ?)",
            (FetchStatus.OK,),
        )
        self._conn.commit()

    # --- snapshot state ---
    def apply(
        self,
        current: dict[str, str],
        obs_index: int,
        ts: str,
        status: FetchStatus,
    ) -> None:
        """Persist the new catalog snapshot and record fetch status.

        CRITICAL (NFR-2 / AC-5): a FAILED fetch is recorded as FAILED, never as
        "unchanged". We do NOT overwrite the stored hashes on failure — that would
        mask real drift as "no change".
        """
        if status == FetchStatus.FAILED:
            self.set_last_fetch_status(FetchStatus.FAILED)
            return  # do not mutate per-server hashes on a failed fetch
        cur = self._conn
        for sid, h in current.items():
            cur.execute(
                "INSERT INTO state (server_id, last_hash, removed) VALUES (?, ?, 0) "
                "ON CONFLICT(server_id) DO UPDATE SET "
                "last_hash = excluded.last_hash, removed = 0",
                (sid, h),
            )
        self.set_last_fetch_status(FetchStatus.OK)
        self._conn.commit()

    def get_last_hash(self, server_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT last_hash FROM state WHERE server_id = ? AND removed = 0",
            (server_id,),
        ).fetchone()
        return row[0] if row else None

    def get_all_hashes(self) -> dict[str, str]:
        """Snapshot of ALL known, non-removed servers: {server_id: last_hash}.

        This is the `prior` input for compute_events. Removed servers are EXCLUDED
        so a RemovalEvent is emitted only ONCE (not on every subsequent poll).
        """
        rows = self._conn.execute(
            "SELECT server_id, last_hash FROM state WHERE removed = 0"
        ).fetchall()
        return {sid: h for sid, h in rows}

    def mark_removed(self, server_id: str) -> None:
        """Flag a server as removed AFTER its RemovalEvent has been emitted once.

        We KEEP the row (preserves last-hash history for audit) but set removed=1
        so get_all_hashes() excludes it and it is not re-reported.
        """
        self._conn.execute(
            "UPDATE state SET removed = 1 WHERE server_id = ?", (server_id,)
        )
        self._conn.commit()

    def get_removed_ids(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT server_id FROM state WHERE removed = 1"
        ).fetchall()
        return {r[0] for r in rows}

    # --- fetch status ---
    def set_last_fetch_status(self, status: FetchStatus) -> None:
        self._conn.execute(
            "INSERT INTO fetch_status (id, status) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET status = excluded.status",
            (status,),
        )
        self._conn.commit()

    def last_fetch_status(self) -> FetchStatus:
        row = self._conn.execute("SELECT status FROM fetch_status WHERE id = 1").fetchone()
        return FetchStatus(row[0])

    # --- event log (append-only) ---
    def append_events(self, events: Iterable) -> None:
        rows = []
        for e in events:
            kind = type(e).__name__
            # payload: stable, comparable representation
            if isinstance(e, (DriftEvent, NewArrivalEvent, RemovalEvent, CatalogEntry)):
                payload = repr(asdict(e))
            else:
                payload = repr(e)
            rows.append((kind, payload))
        if rows:
            self._conn.executemany(
                "INSERT INTO events (kind, payload) VALUES (?, ?)", rows
            )
            self._conn.commit()

    def event_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
