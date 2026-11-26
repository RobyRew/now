"""The accounts on disk: one SQLite row per slot, on the named volume.

A row holds the sealed session (see crypto.py), the name shown on the deck, the Spotify username and the account's
last record: the public fields of the last thing it played, so "latest play" survives a restart. Nothing in a row
is a secret except `session`, which is sealed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SLOTS = range(1, 10)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    slot      INTEGER PRIMARY KEY CHECK (slot BETWEEN 1 AND 9),
    session   BLOB    NOT NULL,
    label     TEXT    NOT NULL DEFAULT '',
    username  TEXT    NOT NULL DEFAULT '',
    added_at  TEXT    NOT NULL,
    record    TEXT,
    record_at REAL
)
"""


@dataclass(frozen=True, slots=True)
class Row:
    slot: int
    session: bytes
    label: str
    username: str
    added_at: str
    record: dict[str, Any] | None
    record_at: float | None


class Store:
    """Small and synchronous: every call is one short local transaction."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # The file holds sealed sessions only, but nobody else on the box needs to read it.
        if not path.exists():
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def rows(self) -> list[Row]:
        with self._lock:
            found = self._db.execute(
                "SELECT slot, session, label, username, added_at, record, record_at FROM accounts ORDER BY slot"
            ).fetchall()
        return [Row(s, bytes(b), lbl, user, added, json.loads(rec) if rec else None, at) for s, b, lbl, user, added, rec, at in found]

    def row(self, slot: int) -> Row | None:
        return next((r for r in self.rows() if r.slot == slot), None)

    def put(self, slot: int, session: bytes, username: str, label: str | None = None) -> None:
        """A new or replaced session for a slot. A replaced one keeps its label unless a new one is given."""
        _check(slot)
        added = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            self._db.execute(
                "INSERT INTO accounts (slot, session, label, username, added_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (slot) DO UPDATE SET session = excluded.session, username = excluded.username, "
                "added_at = excluded.added_at, label = CASE WHEN ? IS NULL THEN accounts.label ELSE excluded.label END, "
                "record = CASE WHEN accounts.username = excluded.username THEN accounts.record END, "
                "record_at = CASE WHEN accounts.username = excluded.username THEN accounts.record_at END",
                (slot, session, label or "", username, added, label),
            )

    def reseal(self, slot: int, session: bytes) -> None:
        with self._lock:
            self._db.execute("UPDATE accounts SET session = ? WHERE slot = ?", (session, slot))

    def set_label(self, slot: int, label: str) -> bool:
        with self._lock:
            return self._db.execute("UPDATE accounts SET label = ? WHERE slot = ?", (label, slot)).rowcount == 1

    def set_username(self, slot: int, username: str) -> None:
        with self._lock:
            self._db.execute("UPDATE accounts SET username = ? WHERE slot = ?", (username, slot))

    def save_record(self, slot: int, record: dict[str, Any], at: float) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE accounts SET record = ?, record_at = ? WHERE slot = ?",
                (json.dumps(record, separators=(",", ":")), at, slot),
            )

    def delete(self, slot: int) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM accounts WHERE slot = ?", (slot,)).rowcount == 1


def _check(slot: int) -> None:
    if slot not in SLOTS:
        raise ValueError("slot must be 1 to 9")
