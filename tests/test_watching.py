"""The Observer overrides (on SpotAPI's real PlayerStatus), the Watcher loop and the Hub, with a fake Spotify."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from now import logs
from now import observer as watching
from now import record as records
from now.crypto import Sealer
from now.hub import Hub, SessionCheckFailed, SessionRefused, cookie_from
from now.store import Store
from tests.fakes import (EXPIRED, FLAKY, GOOD, LEAKY, FakeClient, FakeObserver, cluster, fake_sign_in, fake_username,
                         idle_cluster, until)


class ObserverOverridesTest(unittest.IsolatedAsyncioTestCase):
    """The two changes in observer.Observer, exercised on SpotAPI 2.1.3's own PlayerStatus code."""

    def make(self) -> watching.Observer:
        return watching.Observer(FakeClient("sp_dc=" + GOOD))

    async def test_a_state_is_parsed_and_extras_kept(self) -> None:
        observer = self.make()
        seen: list[Any] = []

        async def on_state(previous: Any, state: Any) -> None:
            seen.append(state)

        observer.add_listener(on_state, "state_update")
        observer._update_cluster(cluster())
        await until(lambda: seen)
        self.assertEqual(seen[0].track.name, "Midnight City")
        self.assertEqual(observer.extras["context_description"], "Deep Focus")
        self.assertEqual(observer.devices[0].name, "Cosmin's iPhone")

    async def test_a_cluster_without_a_player_is_idle(self) -> None:
        observer = self.make()
        idle: list[bool] = []

        async def on_idle() -> None:
            idle.append(True)

        observer.add_listener(on_idle, "idle")
        stamp = int(time.time() * 1000)
        observer._update_cluster(cluster(stamp_ms=stamp))
        observer._update_cluster(idle_cluster(stamp - 5000))  # older than the state held: ignored
        self.assertIsNotNone(observer.state)
        observer._update_cluster(idle_cluster(stamp + 5000))
        await until(lambda: idle)
        self.assertIsNone(observer.state)
        self.assertTrue(observer._snapshot.is_set())

    async def test_no_first_state_keeps_the_connection(self) -> None:
        observer = self.make()
        started = time.monotonic()
        await observer._wait_for_initial_snapshot(0.05)  # SpotAPI raises TimeoutError here
        self.assertLess(time.monotonic() - started, 1)
        observer._disconnected.set()
        with self.assertRaises(ConnectionError):
            await observer._wait_for_initial_snapshot(0.05)


class FakeHub:
    def __init__(self) -> None:
        self.records: list[tuple[int, records.Record]] = []
        self.idle: list[int] = []
        self.usernames: list[tuple[int, str]] = []

    def on_record(self, slot: int, record: records.Record) -> None:
        self.records.append((slot, record))

    def on_idle(self, slot: int) -> None:
        self.idle.append(slot)

    def on_username(self, slot: int, username: str) -> None:
        self.usernames.append((slot, username))

    def on_status(self, slot: int) -> None:
        pass


def capture_logs() -> io.StringIO:
    """The service's real logging setup, writing into a buffer."""
    logs.setup()
    buffer = io.StringIO()
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(buffer)
    return buffer


class WatcherTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeObserver.instances.clear()
        FakeClient.instances.clear()
        self.saved = watching.ERROR_RETRY_MIN, watching.EXPIRED_RETRY
        watching.ERROR_RETRY_MIN, watching.EXPIRED_RETRY = 0.01, 0.05

    def tearDown(self) -> None:
        watching.ERROR_RETRY_MIN, watching.EXPIRED_RETRY = self.saved

    def watcher(self, cookie: str, hub: FakeHub) -> watching.Watcher:
        return watching.Watcher(2, "sp_dc=" + cookie, hub, sign_in=fake_sign_in, username_of=fake_username,
                                observer_cls=FakeObserver)

    async def test_states_become_records(self) -> None:
        hub = FakeHub()
        watcher = self.watcher(GOOD, hub)
        watcher.start()
        await until(lambda: FakeObserver.instances and watcher.status == "idle")
        self.assertEqual(hub.usernames, [(2, "cosmin")])
        from spotapi.models.playback import PlayerState

        observer = FakeObserver.instances[0]
        observer.state = PlayerState.from_cluster(cluster())
        await observer.emit("state_update", None, observer.state)
        self.assertEqual(watcher.status, "live")
        self.assertEqual(hub.records[-1][1].fields["title"], "Midnight City")
        await observer.emit("idle")
        self.assertEqual((watcher.status, hub.idle), ("idle", [2]))
        await watcher.stop()
        self.assertEqual(watcher.status, "stopped")
        self.assertTrue(FakeClient.instances[0].closed)

    async def test_expired_session_waits_and_retries(self) -> None:
        hub = FakeHub()
        watcher = self.watcher(EXPIRED, hub)
        watcher.start()
        await until(lambda: watcher.status == "expired")
        self.assertEqual(watcher.error, "LoginExpired 401")
        await watcher.stop()

    async def test_other_failures_back_off_and_recover(self) -> None:
        hub = FakeHub()
        watcher = self.watcher(FLAKY, hub)
        watcher.start()
        await until(lambda: watcher.status == "error")
        self.assertEqual(watcher.error, "HTTPError 503")
        watcher._cookie = "sp_dc=" + GOOD  # Spotify answers again
        await until(lambda: watcher.status == "idle")
        await watcher.stop()

    async def test_a_dealer_401_inside_start_counts_as_expired(self) -> None:
        from spotapi import LoginExpired

        hub = FakeHub()
        watcher = self.watcher(GOOD, hub)

        class Refused(FakeObserver):
            def __init__(self, client: Any, *, name: str) -> None:
                super().__init__(client, name=name)
                self.fail_with = ExceptionGroup("Connection and cleanup failed", [LoginExpired("no", status_code=401)])

        watcher._observer_cls = Refused
        watcher.start()
        await until(lambda: watcher.status == "expired")
        await watcher.stop()

    async def test_logs_never_carry_the_cookie(self) -> None:
        buffer = capture_logs()
        hub = FakeHub()
        watcher = self.watcher(LEAKY, hub)
        watcher.start()
        await until(lambda: watcher.status == "error")
        await watcher.stop()
        logging.getLogger("spotapi.session").error("Token refresh stopped %s", "sp_dc=" + LEAKY, exc_info=RuntimeError(LEAKY))
        try:
            raise RuntimeError("sp_dc=" + LEAKY)
        except RuntimeError:
            logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
        text = buffer.getvalue()
        self.assertIn("slot 2: stopped (RuntimeError)", text)
        self.assertNotIn(LEAKY, text)


class HubTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeObserver.instances.clear()
        FakeClient.instances.clear()
        self.dir = tempfile.TemporaryDirectory()
        self.db = Path(self.dir.name) / "now.db"
        self.store = Store(self.db)
        self.key = os.urandom(32)
        self.clock = 2_000_000_000.0

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.dir.cleanup()

    def hub(self, key: bytes | None = None, previous: bytes | None = None) -> Hub:
        return Hub(self.store, Sealer(key or self.key, previous), sign_in=fake_sign_in, username_of=fake_username,
                   watcher_options={"observer_cls": FakeObserver}, clock=lambda: self.clock)

    def put(self, hub: Hub, slot: int, *, playing: bool, at: float, title: str, ends: float | None = None) -> None:
        hub._watchers[slot] = watching.Watcher(slot, "x", hub)  # never started; makes the slot count as watched
        fields = {k: "" for k in records.FIELDS} | {"playing": playing, "paused": False, "title": title, "account": slot}
        hub._records[slot] = records.Record(fields=fields, at=at, ends_at=ends)

    async def test_first_playing_wins_else_latest_else_lower_slot(self) -> None:
        hub = self.hub()
        self.put(hub, 1, playing=False, at=self.clock - 60, title="older")
        self.put(hub, 3, playing=True, at=self.clock - 30, title="live", ends=self.clock + 100)
        self.put(hub, 2, playing=True, at=self.clock - 30, title="also live", ends=self.clock + 100)
        self.assertEqual(hub.now()["now"]["title"], "also live")
        hub._records.pop(2)
        hub._records.pop(3)
        self.put(hub, 4, playing=False, at=self.clock - 10, title="newest")
        self.assertEqual(hub.now()["now"]["title"], "newest")
        self.put(hub, 5, playing=False, at=self.clock - 10, title="same time")
        self.assertEqual(hub.now()["now"]["title"], "newest")
        self.put(hub, 6, playing=True, at=self.clock - 900, title="stale", ends=self.clock - 600)
        body = hub.now()
        self.assertEqual(body["now"]["title"], "newest")  # its play ended 10 minutes ago
        self.assertEqual(body["at"], "2033-05-18T03:33:20Z")
        self.assertEqual(body["live"], 0)

    async def test_add_checks_seals_and_watches(self) -> None:
        hub = self.hub()
        with self.assertRaises(ValueError):
            await hub.add(1, "not a cookie")
        with self.assertRaises(SessionRefused):
            await hub.add(1, EXPIRED)
        with self.assertRaises(SessionCheckFailed):
            await hub.add(1, f"sp_t=abc; sp_dc={FLAKY}")
        added = await hub.add(1, f"sp_t=abc; sp_dc={GOOD}; sp_key=zzz")
        self.assertEqual((added["slot"], added["username"]), (1, "cosmin"))
        self.assertNotIn(GOOD, repr(hub.accounts()))
        self.assertNotIn(GOOD.encode(), self.db.read_bytes())
        await until(lambda: FakeObserver.instances and hub._watchers[1].status == "idle")
        self.assertEqual(len(FakeClient.instances), 1)  # the checked client is reused, not a second sign-in
        self.assertEqual(FakeClient.instances[0].cookie, f"sp_dc={GOOD}")
        self.assertTrue(hub.rename(1, "  Cosmin\n "))
        self.assertEqual(hub.accounts()[0]["label"], "Cosmin")
        self.assertTrue(await hub.remove(1))
        self.assertTrue(hub.accounts()[0]["empty"])
        await hub.close()

    async def test_start_reseals_old_key_and_flags_unopenable_rows(self) -> None:
        old = os.urandom(32)
        self.store.put(1, Sealer(old).seal("sp_dc=" + GOOD, 1), "cosmin", label="Cosmin")
        self.store.put(2, Sealer(os.urandom(32)).seal("sp_dc=" + GOOD, 2), "lost")
        hub = self.hub(previous=old)
        await hub.start()
        self.assertEqual(Sealer(self.key).open(self.store.row(1).session, 1), ("sp_dc=" + GOOD, False))  # type: ignore[union-attr]
        statuses = {a["slot"]: a.get("status") for a in hub.accounts()}
        self.assertEqual(statuses[2], "locked")
        await until(lambda: hub._watchers[1].status == "idle")
        await hub.close()

    async def test_idle_ends_a_play(self) -> None:
        hub = self.hub()
        self.put(hub, 1, playing=True, at=self.clock - 30, title="t", ends=self.clock + 100)
        self.store.put(1, b"x", "u")
        hub.on_idle(1)
        view = hub.now()["now"]
        self.assertEqual((view["playing"], view["when"]), (False, records.iso_minute(self.clock)))

    def test_cookie_from(self) -> None:
        self.assertEqual(cookie_from(GOOD), f"sp_dc={GOOD}")
        self.assertEqual(cookie_from(f" sp_dc={GOOD} "), f"sp_dc={GOOD}")
        self.assertEqual(cookie_from(f"a=b; sp_dc={GOOD}; c=d"), f"sp_dc={GOOD}")
        self.assertIsNone(cookie_from("a=b; c=d"))
        self.assertIsNone(cookie_from("short"))
        self.assertIsNone(cookie_from("sp_dc=has spaces and ; semicolons"))


if __name__ == "__main__":
    unittest.main()
