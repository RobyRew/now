"""One Spotify account, watched for as long as the service runs.

`Observer` is SpotAPI's read-only Connect observer (`PlayerStatus`; `Player`, which adds playback commands, is never
imported here) with two changes for an always-on service:

- SpotAPI's `connect()` waits up to 15 s for a first player state and fails without one, and `start()` then
  reconnects, every 30 s at most. An account with nothing loaded on any device sends no player state, so it would
  reconnect around the clock. Here a missing first state does not fail the connection: the socket stays open and
  Spotify pushes the next state when a device starts.
- A Connect cluster with no active device and no track is passed on as an `idle` event (SpotAPI ignores clusters
  without a player state and keeps the last one), so a player that went away stops showing as playing.

It also keeps two values SpotAPI's parsed state drops, `artist_uri` and `context_description`, when a state carries
them.

`Watcher` runs one account: signs in with the account's cookie, runs `Observer.start()` (which reconnects with backoff
on network errors and HTTP 408/429/5xx), and handles what `start()` does not: an expired session (HTTP 401) waits
for a new cookie, retried every EXPIRED_RETRY seconds, and other failures are retried with a growing pause.

Logging: only the slot, the event, the error type and the HTTP status. Error messages and URLs can carry a cookie
or a token (the dealer URL has the access token in its query string), so they are never logged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from spotapi import Account, AsyncClient, Catalogue, LoginExpired
from spotapi.status import PlayerStatus
from spotapi.types import HTTPError

from . import record as records

log = logging.getLogger("now")

OBSERVER_NAME = "robyrew-now"
EXPIRED_RETRY = 6 * 3600  # seconds between sign-in attempts once a session has expired
ERROR_RETRY_MIN = 30  # first pause after another failure; doubles up to ERROR_RETRY_MAX
ERROR_RETRY_MAX = 30 * 60
STABLE_AFTER = 600  # a connection that lasted this long resets the failure count
DETAILS_CACHE = 64  # track URIs whose catalogue details are kept per account


class Hub(Protocol):
    """What a Watcher reports to (hub.Hub)."""

    def on_record(self, slot: int, record: records.Record) -> None: ...
    def on_idle(self, slot: int) -> None: ...
    def on_username(self, slot: int, username: str) -> None: ...
    def on_status(self, slot: int) -> None: ...


SignIn = Callable[[str], Awaitable[Any]]


async def sign_in(cookie: str) -> AsyncClient:
    """An open, signed-in SpotAPI client for a `sp_dc=...` cookie. Its token renewal runs inside it."""
    return await AsyncClient.from_cookies(cookie)


async def username_of(client: Any) -> str:
    """The account's username. The profile also carries the e-mail address, which is not kept."""
    return (await Account(client).get_profile()).username


def is_expired(error: BaseException) -> bool:
    """Whether a failure, or anything it groups or was caused by, is Spotify refusing the session."""
    stack, seen = [error], set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, LoginExpired) or (isinstance(current, HTTPError) and current.status_code == 401):
            return True
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        if current.__cause__ is not None:
            stack.append(current.__cause__)
    return False


def describe(error: BaseException) -> str:
    """An error as its type name and HTTP status: nothing from its message."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    status = getattr(error, "status_code", None)
    return f"{type(error).__name__} {status}" if isinstance(status, int) else type(error).__name__


def _integer(value: object) -> int:
    """A protocol integer, as SpotAPI reads them: Connect JSON sends timestamps as numbers or numeric strings."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def extras_of(player: Mapping[str, Any]) -> dict[str, str]:
    """`artist_uri` and `context_description` from a raw player state, when present."""
    found: dict[str, str] = {}
    track = player.get("track")
    metadata = track.get("metadata") if isinstance(track, Mapping) else None
    if isinstance(metadata, Mapping) and isinstance(metadata.get("artist_uri"), str):
        found["artist_uri"] = metadata["artist_uri"]
    context = player.get("context_metadata")
    if isinstance(context, Mapping) and isinstance(context.get("context_description"), str):
        description = context["context_description"].strip()
        if description:
            found["context_description"] = description[:200]
    return found


class Observer(PlayerStatus):
    """PlayerStatus for an always-on service; see the module docstring for the two changes."""

    def __init__(self, session: Any, *, name: str = OBSERVER_NAME) -> None:
        super().__init__(session, name=name)
        self.extras: dict[str, str] = {}

    async def _wait_for_initial_snapshot(self, timeout: float) -> None:
        try:
            await super()._wait_for_initial_snapshot(timeout)
        except TimeoutError:
            if self._disconnected.is_set():
                raise
            # Nothing loaded on any device: stay connected, the next state arrives by push.

    def _update_cluster(self, cluster: Mapping[str, Any]) -> None:
        player = cluster.get("player_state")
        track = player.get("track") if isinstance(player, Mapping) else None
        if not track and not cluster.get("active_device_id"):
            if self.state is not None and _integer(cluster.get("timestamp")) < self.state.timestamp:
                return  # older than the state already held
            self.state = None
            self.extras = {}
            self._snapshot.set()
            self._events.dispatch("idle")
            return
        if isinstance(player, Mapping):
            self.extras = extras_of(player)
        super()._update_cluster(cluster)


class Watcher:
    """Keeps one account's Observer running until stop()."""

    def __init__(
        self,
        slot: int,
        cookie: str,
        hub: Hub,
        *,
        label: str = "",
        username: str = "",
        client: Any = None,
        sign_in: SignIn = sign_in,
        username_of: Callable[[Any], Awaitable[str]] = username_of,
        observer_cls: type[Any] = Observer,
        catalogue_cls: type[Any] = Catalogue,
    ) -> None:
        self.slot = slot
        self.label = label
        self.username = username
        self.status = "connecting"
        self.error = ""
        self.since = time.time()
        self._cookie = cookie
        self._hub = hub
        self._handover = client  # an already signed-in client (from the admin page's check), used once
        self._sign_in = sign_in
        self._username_of = username_of
        self._observer_cls = observer_cls
        self._catalogue_cls = catalogue_cls
        self._observer: Any = None
        self._stopping = False
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._details: OrderedDict[str, tuple[str, str, str, str]] = OrderedDict()
        self._logged: tuple[str, float] = ("", 0.0)

    @property
    def connected(self) -> bool:
        return self.status in ("live", "idle")

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"now-watch-{self.slot}")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        observer, task = self._observer, self._task
        if observer is not None:
            try:
                await asyncio.wait_for(observer.close(), 10)
            except Exception as error:  # closing is best effort; the task below is cancelled if it hangs
                log.info("slot %d: close failed (%s)", self.slot, describe(error))
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), 10)
            except (TimeoutError, Exception):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self._handover is not None:
            await _close_quietly(self._handover)
            self._handover = None
        self._set("stopped")

    # ── the loop ────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        failures = 0
        while not self._stopping:
            started = time.monotonic()
            try:
                self._set("connecting")
                client = self._handover or await self._sign_in(self._cookie)
                self._handover = None
                async with client:
                    if not self.username:
                        await self._find_username(client)
                    observer = self._observer_cls(client, name=OBSERVER_NAME)
                    for name, listener in (("ready", self._ready), ("state_update", self._state), ("idle", self._idle),
                                           ("disconnect", self._disconnected), ("reconnect", self._reconnected),
                                           ("error", self._error)):
                        observer.add_listener(listener, name)
                    self._observer = observer
                    if self._stopping:
                        break
                    await observer.start()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._observer = None
                if self._stopping:
                    break
                if is_expired(error):
                    self._set("expired", describe(error))
                    log.info("slot %d: session refused (%s); waiting for a new sp_dc", self.slot, describe(error))
                    await self._pause(EXPIRED_RETRY)
                    continue
                if time.monotonic() - started > STABLE_AFTER:
                    failures = 0
                failures += 1
                pause = min(ERROR_RETRY_MIN * 2 ** (failures - 1), ERROR_RETRY_MAX)
                self._set("error", describe(error))
                log.info("slot %d: stopped (%s); trying again in %d s", self.slot, describe(error), pause)
                await self._pause(pause)
            finally:
                self._observer = None

    async def _pause(self, seconds: float) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except TimeoutError:
            pass

    async def _find_username(self, client: Any) -> None:
        try:
            username = await self._username_of(client)
        except Exception as error:
            log.info("slot %d: no profile name (%s)", self.slot, describe(error))
            return
        if username:
            self.username = username
            self._hub.on_username(self.slot, username)

    def _set(self, status: str, error: str = "") -> None:
        if status != self.status or error != self.error:
            self.status, self.error, self.since = status, error, time.time()
            self._hub.on_status(self.slot)

    # ── observer events (each runs in its own task) ─────────────────────────

    async def _ready(self) -> None:
        observer = self._observer
        has_track = observer is not None and observer.state is not None and observer.state.track is not None
        self._set("live" if has_track else "idle")
        log.info("slot %d: connected (%s)", self.slot, self.status)

    async def _state(self, previous: Any, state: Any) -> None:
        observer = self._observer
        if observer is None or state is None:
            return
        details = None
        if state.track is not None and not (state.track.name and state.track.artist):
            details = await self._track_details(observer, state.track.uri)
        made = records.build(state, observer.devices, observer.extras, slot=self.slot, label=self.label,
                             username=self.username, details=details)
        if made is not None and observer is self._observer:
            self._set("live")
            self._hub.on_record(self.slot, made)

    async def _idle(self) -> None:
        self._set("idle")
        self._hub.on_idle(self.slot)

    async def _disconnected(self) -> None:
        if not self._stopping:
            self._set("reconnecting")

    async def _reconnected(self) -> None:
        log.info("slot %d: reconnected", self.slot)

    async def _error(self, error: BaseException) -> None:
        # start() retries these itself; log each kind at most once a minute.
        kind = describe(error)
        last, at = self._logged
        if kind != last or time.monotonic() - at > 60:
            self._logged = (kind, time.monotonic())
            log.info("slot %d: connection problem (%s)", self.slot, kind)

    async def _track_details(self, observer: Any, uri: str) -> tuple[str, str, str, str] | None:
        if not uri.startswith("spotify:track:"):
            return None
        if uri in self._details:
            self._details.move_to_end(uri)
            return self._details[uri]
        try:
            async with self._catalogue_cls(observer.session) as catalogue:
                details = records.details_fields(await catalogue.get_track(uri))
        except Exception as error:
            log.info("slot %d: no track details (%s)", self.slot, describe(error))
            return None
        self._details[uri] = details
        if len(self._details) > DETAILS_CACHE:
            self._details.popitem(last=False)
        return details


async def _close_quietly(client: Any) -> None:
    try:
        await client.close()
    except Exception as error:
        log.info("client close failed (%s)", describe(error))
