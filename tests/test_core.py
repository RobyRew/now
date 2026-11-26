"""Sealing, the store, and building records from Connect states."""

from __future__ import annotations

import base64
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from spotapi.models.playback import Device, PlayerState

from now import record as records
from now.config import ConfigError, Settings
from now.crypto import Sealer, SealError
from now.store import Store
from tests.fakes import GOOD, cluster

KEY, OTHER = os.urandom(32), os.urandom(32)


class ConfigTest(unittest.TestCase):
    def test_import_token_is_off_or_long(self) -> None:
        base = {"SESSION_SECRET": "s" * 48, "NOW_KEY": base64.b64encode(os.urandom(32)).decode()}
        with mock.patch.dict(os.environ, base | {"IMPORT_TOKEN": "short"}, clear=True):
            with self.assertRaises(ConfigError):
                Settings.from_env()
        with mock.patch.dict(os.environ, base, clear=True):
            self.assertEqual(Settings.from_env().import_token, "")
        with mock.patch.dict(os.environ, base | {"IMPORT_TOKEN": "k" * 40}, clear=True):
            self.assertEqual(Settings.from_env().import_token, "k" * 40)


class SealerTest(unittest.TestCase):
    def test_round_trip_and_fresh_nonce(self) -> None:
        sealer = Sealer(KEY)
        a, b = sealer.seal("sp_dc=" + GOOD, 3), sealer.seal("sp_dc=" + GOOD, 3)
        self.assertNotEqual(a, b)
        self.assertNotIn(GOOD.encode(), a)
        self.assertEqual(sealer.open(a, 3), ("sp_dc=" + GOOD, False))

    def test_other_slot_or_damage_does_not_open(self) -> None:
        sealer = Sealer(KEY)
        sealed = sealer.seal("secret", 3)
        with self.assertRaises(SealError):
            sealer.open(sealed, 4)
        with self.assertRaises(SealError):
            sealer.open(sealed[:-1] + bytes([sealed[-1] ^ 1]), 3)
        with self.assertRaises(SealError):
            Sealer(OTHER).open(sealed, 3)

    def test_previous_key_opens_and_asks_for_a_reseal(self) -> None:
        old = Sealer(OTHER).seal("secret", 1)
        self.assertEqual(Sealer(KEY, previous=OTHER).open(old, 1), ("secret", True))


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "now.db"
        self.store = Store(self.path)
        self.sealer = Sealer(KEY)

    def tearDown(self) -> None:
        self.store.close()
        self.dir.cleanup()

    def test_file_is_private_and_holds_no_plaintext_session(self) -> None:
        self.store.put(2, self.sealer.seal("sp_dc=" + GOOD, 2), "cosmin")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertNotIn(GOOD.encode(), self.path.read_bytes())

    def test_replace_keeps_label_and_drops_another_accounts_record(self) -> None:
        self.store.put(1, b"x", "cosmin", label="Cosmin")
        self.store.save_record(1, {"fields": {"title": "a"}, "at": 1.0}, 1.0)
        self.store.put(1, b"y", "cosmin")
        row = self.store.row(1)
        assert row is not None
        self.assertEqual((row.label, row.session, row.record), ("Cosmin", b"y", {"fields": {"title": "a"}, "at": 1.0}))
        self.store.put(1, b"z", "someone-else")
        row = self.store.row(1)
        assert row is not None
        self.assertEqual((row.label, row.record), ("Cosmin", None))

    def test_slots_are_one_to_nine(self) -> None:
        with self.assertRaises(ValueError):
            self.store.put(10, b"x", "u")
        self.assertTrue(self.store.delete(1) is False)


def state_of(raw: dict) -> tuple[PlayerState, tuple[Device, ...]]:
    state = PlayerState.from_cluster(raw)
    devices = tuple(Device(i, v["name"], v["device_type"]) for i, v in raw["devices"].items())
    return state, devices


class RecordTest(unittest.TestCase):
    def test_fields_follow_data_py(self) -> None:
        state, devices = state_of(cluster(stamp_ms=1_795_700_000_000))
        extras = {"artist_uri": "spotify:artist:63MQldklfxkjYDoUE4Tppz", "context_description": "Deep Focus"}
        made = records.build(state, devices, extras, slot=2, label="", username="cosmin")
        assert made is not None
        self.assertEqual(tuple(made.fields), records.FIELDS)
        f = made.fields
        self.assertEqual((f["playing"], f["paused"], f["title"], f["artist"]), (True, False, "Midnight City", "M83"))
        self.assertEqual((f["context"], f["context_kind"]), ("Deep Focus", "Playlist"))
        self.assertEqual((f["device"], f["device_type"]), ("Cosmin's iPhone", "smartphone"))
        self.assertEqual(f["when"], "2026-11-26T13:33+00:00")
        self.assertEqual(f["song_url"], "https://open.spotify.com/track/abc123")
        self.assertEqual(f["play_url"], f["song_url"])
        self.assertEqual(f["artist_url"], "https://open.spotify.com/artist/63MQldklfxkjYDoUE4Tppz")
        self.assertEqual(f["context_url"], "https://open.spotify.com/playlist/37i9dQZF1DX")
        self.assertEqual((f["user"], f["user_url"], f["account"]), ("cosmin", "https://open.spotify.com/user/cosmin", 2))
        assert made.ends_at is not None
        self.assertAlmostEqual(made.ends_at - time.time(), 210, delta=5)

    def test_label_paused_and_no_device(self) -> None:
        state, devices = state_of(cluster(playing=False, paused=True))
        made = records.build(state, devices, {}, slot=1, label="Cosmin", username="cosmin")
        assert made is not None
        self.assertEqual((made.fields["playing"], made.fields["paused"], made.fields["user"]), (False, True, "Cosmin"))
        self.assertEqual(made.fields["context"], "A playlist")  # no description in extras
        state, devices = state_of(cluster(device=None))
        made = records.build(state, devices, {}, slot=1, label="", username="u")
        assert made is not None
        self.assertFalse(made.fields["playing"])

    def test_contexts(self) -> None:
        self.assertEqual(records.context_of("spotify:user:x:collection", "A"), ("Library", "Liked Songs", "https://open.spotify.com/collection/tracks"))
        self.assertEqual(records.context_of("spotify:album:1", "Album"), ("Album", "Album", "https://open.spotify.com/album/1"))
        self.assertEqual(records.context_of(None, ""), ("", "", ""))

    def test_shown_ages_plays_and_pauses(self) -> None:
        now = 1_000_000.0
        playing = records.Record(fields={"playing": True, "paused": False, "when": ""}, at=now - 100, ends_at=now + 10)
        self.assertTrue(records.shown(playing, now)["playing"])
        self.assertTrue(records.shown(playing, now + 10 + records.GRACE - 1)["playing"])
        late = records.shown(playing, now + 10 + records.GRACE + 1)
        self.assertFalse(late["playing"])
        self.assertEqual(late["when"], records.iso_minute(now + 10))
        paused = records.Record(fields={"playing": False, "paused": True, "when": ""}, at=now)
        self.assertTrue(records.shown(paused, now + records.PAUSED_FOR - 1)["paused"])
        self.assertFalse(records.shown(paused, now + records.PAUSED_FOR + 1)["paused"])

    def test_json_round_trip_keeps_only_deck_fields(self) -> None:
        made = records.Record(fields={"title": "t", "cookie": "nope"}, at=5.0, ends_at=None, track_uri="spotify:track:x")
        back = records.Record.from_json(made.to_json())
        assert back is not None
        self.assertEqual(back.fields, {"title": "t"})


if __name__ == "__main__":
    unittest.main()
