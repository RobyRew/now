"""All accounts together: their watchers, their latest records, and what /api/now shows."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from . import observer as watching
from . import record as records
from .crypto import Sealer, SealError
from .store import SLOTS, Store

log = logging.getLogger("now")

LABEL_MAX = 40
_SP_DC = re.compile(r"[A-Za-z0-9_\-]{20,1000}")


class SessionRefused(Exception):
    """Spotify did not accept a pasted session."""


class SessionCheckFailed(Exception):
    """Spotify could not be asked about a pasted session (network, rate limit, outage). The type name is in args."""


def cookie_from(text: str) -> str | None:
    """`sp_dc=<value>` from what was pasted: the bare value, `sp_dc=<value>`, or a whole Cookie header (everything
    other than sp_dc is dropped). None when there is no plausible sp_dc in it."""
    text = (text or "").strip()
    if not text:
        return None
    value: str | None = text
    if "=" in text:
        value = None
        for part in text.split(";"):
            name, _, candidate = part.strip().partition("=")
            if name.strip() == "sp_dc":
                value = candidate
    if value is None:
        return None
    value = value.strip().strip('"')
    return f"sp_dc={value}" if _SP_DC.fullmatch(value) else None


def clean_label(text: str) -> str:
    """A deck name: printable, single-line, at most LABEL_MAX characters."""
    return " ".join("".join(c for c in (text or "") if c.isprintable()).split())[:LABEL_MAX]


class Hub:
    """Owns the watchers; the web layer only calls the public methods below."""

    def __init__(
        self,
        store: Store,
        sealer: Sealer,
        *,
        sign_in: Callable[[str], Awaitable[Any]] = watching.sign_in,
        username_of: Callable[[Any], Awaitable[str]] = watching.username_of,
        watcher_options: dict[str, Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._sealer = sealer
        self._sign_in = sign_in
        self._username_of = username_of
        self._watcher_options = watcher_options or {}
        self._clock = clock
        self._watchers: dict[int, watching.Watcher] = {}
        self._records: dict[int, records.Record] = {}
        self._labels: dict[int, str] = {}
        self._usernames: dict[int, str] = {}
        self._locked: set[int] = set()  # rows whose session no configured key opens
        self._lock = asyncio.Lock()

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        for row in self._store.rows():
            self._labels[row.slot] = row.label
            self._usernames[row.slot] = row.username
            if row.record:
                kept = records.Record.from_json(row.record)
                if kept is not None:
                    kept.fields["user"] = row.label or row.username
                    self._records[row.slot] = kept
            try:
                cookie, stale = self._sealer.open(row.session, row.slot)
            except SealError:
                self._locked.add(row.slot)
                log.info("slot %d: the stored session does not open with NOW_KEY or NOW_KEY_PREVIOUS", row.slot)
                continue
            if stale:
                self._store.reseal(row.slot, self._sealer.seal(cookie, row.slot))
                log.info("slot %d: session sealed again under the current key", row.slot)
            self._start_watcher(row.slot, cookie)
        log.info("watching %d account(s)", len(self._watchers))

    async def close(self) -> None:
        watchers, self._watchers = list(self._watchers.values()), {}
        await asyncio.gather(*(w.stop() for w in watchers), return_exceptions=True)

    def _start_watcher(self, slot: int, cookie: str, client: Any = None) -> None:
        watcher = watching.Watcher(
            slot, cookie, self, label=self._labels.get(slot, ""), username=self._usernames.get(slot, ""),
            client=client, sign_in=self._sign_in, username_of=self._username_of, **self._watcher_options,
        )
        self._watchers[slot] = watcher
        watcher.start()

    # ── what /api/now shows ─────────────────────────────────────────────────

    def now(self) -> dict[str, Any]:
        """The first account playing (slot order), else the latest play; `live` = observers connected now."""
        now = self._clock()
        views = [(slot, record, records.shown(record, now)) for slot, record in sorted(self._records.items())]
        pick = next((view for _, _, view in views if view["playing"]), None)
        if pick is None and views:
            pick = max(views, key=lambda item: (records.last_seen(item[1], now), -item[0]))[2]
        return {
            "now": pick,
            "live": sum(1 for w in self._watchers.values() if w.connected),
            "at": datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }

    # ── the admin page ──────────────────────────────────────────────────────

    def accounts(self) -> list[dict[str, Any]]:
        """Every slot, for the admin page. Never includes a session."""
        now = self._clock()
        out = []
        for slot in SLOTS:
            watcher = self._watchers.get(slot)
            if watcher is None and slot not in self._locked:
                out.append({"slot": slot, "empty": True})
                continue
            record = self._records.get(slot)
            view = records.shown(record, now) if record else None
            out.append({
                "slot": slot,
                "empty": False,
                "label": self._labels.get(slot, ""),
                "username": self._usernames.get(slot, ""),
                "status": "locked" if slot in self._locked else watcher.status if watcher else "stopped",
                "error": watcher.error if watcher else "",
                "since": _iso(watcher.since) if watcher else "",
                "last": None if view is None else {
                    "title": view["title"], "artist": view["artist"], "playing": view["playing"],
                    "paused": view["paused"], "when": view["when"], "device": view["device"],
                },
            })
        return out

    async def add(self, slot: int, pasted: str) -> dict[str, Any]:
        """Check a pasted session with Spotify, then seal it into the slot and watch it.

        Raises ValueError (nothing usable pasted), SessionRefused (Spotify said no) or SessionCheckFailed."""
        if slot not in SLOTS:
            raise ValueError("slot")
        cookie = cookie_from(pasted)
        if cookie is None:
            raise ValueError("sp_dc")
        try:
            client = await self._sign_in(cookie)
        except Exception as error:
            if watching.is_expired(error):
                raise SessionRefused(watching.describe(error)) from None
            raise SessionCheckFailed(watching.describe(error)) from None
        try:
            username = await self._username_of(client)
        except Exception as error:
            await watching._close_quietly(client)
            if watching.is_expired(error):
                raise SessionRefused(watching.describe(error)) from None
            raise SessionCheckFailed(watching.describe(error)) from None
        async with self._lock:
            old = self._watchers.pop(slot, None)
            if old is not None:
                await old.stop()
            if self._usernames.get(slot) != username:
                self._records.pop(slot, None)
            self._store.put(slot, self._sealer.seal(cookie, slot), username)
            self._usernames[slot] = username
            self._labels.setdefault(slot, "")
            self._locked.discard(slot)
            self._start_watcher(slot, cookie, client)
        log.info("slot %d: session added", slot)
        return next(a for a in self.accounts() if a["slot"] == slot)

    async def remove(self, slot: int) -> bool:
        async with self._lock:
            watcher = self._watchers.pop(slot, None)
            if watcher is not None:
                await watcher.stop()
            self._records.pop(slot, None)
            self._labels.pop(slot, None)
            self._usernames.pop(slot, None)
            self._locked.discard(slot)
            removed = self._store.delete(slot)
        if removed:
            log.info("slot %d: removed", slot)
        return removed

    def rename(self, slot: int, label: str) -> bool:
        label = clean_label(label)
        if not self._store.set_label(slot, label):
            return False
        self._labels[slot] = label
        watcher = self._watchers.get(slot)
        if watcher is not None:
            watcher.label = label
        record = self._records.get(slot)
        if record is not None:
            record.fields["user"] = label or self._usernames.get(slot, "")
            self._store.save_record(slot, record.to_json(), record.at)
        return True

    # ── reports from the watchers ───────────────────────────────────────────

    def on_record(self, slot: int, record: records.Record) -> None:
        if slot not in self._watchers:
            return
        previous = self._records.get(slot)
        self._records[slot] = record
        if previous is None or previous.fields != record.fields:
            self._store.save_record(slot, record.to_json(), record.at)

    def on_idle(self, slot: int) -> None:
        """Nothing is loaded on any of the account's devices: its last record stops playing or being paused."""
        record = self._records.get(slot)
        if record is None or not (record.fields.get("playing") or record.fields.get("paused")):
            return
        now = self._clock()
        ended = min(record.ends_at or now, now) if record.fields.get("playing") else record.at
        record.fields.update(playing=False, paused=False, when=records.iso_minute(ended))
        record.at, record.ends_at = ended, None
        self._store.save_record(slot, record.to_json(), record.at)

    def on_username(self, slot: int, username: str) -> None:
        if slot in self._watchers and username != self._usernames.get(slot):
            self._usernames[slot] = username
            self._store.set_username(slot, username)

    def on_status(self, slot: int) -> None:
        """A watcher's status changed; the admin page reads it on its next refresh."""


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
