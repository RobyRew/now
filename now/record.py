"""The record player's fields for one account, built from its Spotify Connect state.

The fields and their rules follow `.github/profile/data.py` in RobyRew/RobyRew (`spotify_observe` and its helpers), so
the deck reads the same whether its Spotify part comes from this service or from the profile's own workflow.

A snapshot taken every few minutes ages out by itself; a record kept by an always-on observer does not. So `shown()`
decides at read time whether a record still counts as playing or paused:
- playing: until the track's projected end plus a grace period. Any later state (the next track, a pause) replaces
  the record, so a "playing" record past its end means the player went away without saying so.
- paused: for PAUSED_FOR seconds after the pause; after that it shows as the last play.
"""

from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

GRACE = 120  # seconds a "playing" record outlives its track's projected end
PAUSED_FOR = 30 * 60  # seconds a pause shows as PAUSED before it shows as the last play

PLAY_SEARCH = "https://open.spotify.com/search/{q}"
CONTEXT_KINDS = {"album": "Album", "playlist": "Playlist", "artist": "Radio", "collection": "Library", "show": "Podcast"}
CONTEXT_NAMES = {"collection": "Liked Songs", "playlist": "A playlist", "artist": "Artist radio", "show": "A podcast"}

# The record player's fields, in the order data.py writes them. Nothing else leaves the service.
FIELDS = (
    "playing", "paused", "title", "artist", "cover", "context", "context_kind", "device", "device_type", "user",
    "platform", "when", "account", "song_url", "artist_url", "context_url", "user_url", "play_url",
)


@dataclass(slots=True)
class Record:
    """One account's latest play: the public fields, plus the times `shown()` needs."""

    fields: dict[str, Any]
    at: float
    """Epoch seconds of the Connect state this came from."""
    ends_at: float | None = None
    """Epoch seconds when the track would end if it keeps playing; None when not playing."""
    track_uri: str = field(default="", compare=False)

    def to_json(self) -> dict[str, Any]:
        return {"fields": self.fields, "at": self.at, "ends_at": self.ends_at, "track_uri": self.track_uri}

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> Record | None:
        fields = value.get("fields")
        if not isinstance(fields, dict) or not isinstance(value.get("at"), (int, float)):
            return None
        ends = value.get("ends_at")
        return cls(
            fields={k: fields[k] for k in FIELDS if k in fields},
            at=float(value["at"]),
            ends_at=float(ends) if isinstance(ends, (int, float)) else None,
            track_uri=str(value.get("track_uri") or ""),
        )


def open_url(uri: str | None) -> str:
    """A Spotify URI as an open.spotify.com address: spotify:track:ID becomes /track/ID, Liked Songs /collection."""
    parts = (uri or "").split(":")
    if len(parts) == 3 and parts[0] == "spotify":
        return f"https://open.spotify.com/{parts[1]}/{parts[2]}"
    if len(parts) >= 4 and parts[0] == "spotify" and parts[1] == "user" and parts[3] == "collection":
        return "https://open.spotify.com/collection/tracks"
    return ""


def context_of(uri: str | None, album: str, description: str = "") -> tuple[str, str, str]:
    """What the song plays from, as (kind, name, url). The Connect state names the album; other contexts get a kind,
    and their own name when the state carries one (`description`)."""
    parts = (uri or "").split(":")
    kind = "collection" if "collection" in parts else (parts[1] if len(parts) >= 3 else "")
    if kind in ("", "album"):
        return ("Album", album, open_url(uri)) if album else ("", "", "")
    name = description if kind in ("playlist", "show") and description else CONTEXT_NAMES.get(kind, "")
    return CONTEXT_KINDS.get(kind, ""), name, open_url(uri)


def details_fields(details: Any) -> tuple[str, str, str, str]:
    """Title, artists, album and a cover from SpotAPI's TrackDetails, for when the Connect state leaves them out."""
    album = getattr(details, "album_of_track", None)
    sources = getattr(getattr(album, "cover_art", None), "sources", None) or []
    sized = sorted((s for s in sources if getattr(s, "url", "")), key=lambda s: abs((getattr(s, "width", 0) or 0) - 300))
    artists = []
    for group in (getattr(details, "first_artist", None), getattr(details, "other_artists", None)):
        for item in getattr(group, "items", None) or []:
            name = getattr(getattr(item, "profile", None), "name", "")
            if name:
                artists.append(name)
    return getattr(details, "name", "") or "", ", ".join(artists), getattr(album, "name", "") or "", sized[0].url if sized else ""


def iso_minute(seconds: float) -> str:
    """The ISO minute the deck shows, as data.py writes it: 2026-11-26T15:04+00:00."""
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="minutes")


def stamp_seconds(stamp: int | None) -> float:
    """A Connect timestamp (milliseconds, or seconds from older payloads) as epoch seconds; now when missing."""
    if not stamp:
        return time.time()
    return stamp / 1000 if stamp > 10**11 else float(stamp)


def build(state: Any, devices: Any, extras: dict[str, str], *, slot: int, label: str, username: str,
          details: tuple[str, str, str, str] | None = None, now: float | None = None) -> Record | None:
    """The record for one Connect state (SpotAPI's PlayerState), or None when nothing is loaded on any device.

    `extras` holds values SpotAPI's parsed state drops (see observer.Observer): `artist_uri` and
    `context_description`, each only when the state carried it. `details` fills a missing title or artist.
    """
    track = getattr(state, "track", None)
    if track is None:
        return None
    title, artist, album, cover = track.name, track.artist, track.album, track.image_url or ""
    if details and not (title and artist):
        title, artist, album, cover = (new or old for new, old in zip(details, (title, artist, album, cover), strict=True))
    if not title:
        return None
    now = time.time() if now is None else now
    at = stamp_seconds(getattr(state, "timestamp", 0))
    # Without an active device nothing is actually playing, whatever the last state said.
    playing = bool(state.is_playing and not state.is_paused and state.active_device_id)
    ends_at = None
    if playing:
        ends_at = now + max(0, getattr(state, "remaining_ms", 0)) / 1000 / max(getattr(state, "playback_speed", 1.0) or 1.0, 0.25)
    device = next((d for d in devices or () if d.id == state.active_device_id), None)
    kind, name, context_url = context_of(state.context_uri, album, extras.get("context_description", ""))
    song = open_url(track.uri)
    artist_url = open_url(extras.get("artist_uri")) if extras.get("artist_uri", "").startswith("spotify:artist:") else ""
    fields = {
        "playing": playing,
        "paused": bool(state.is_paused) and not playing,
        "title": title,
        "artist": artist,
        "cover": cover,
        "context": name,
        "context_kind": kind,
        "device": device.name if device else "",
        "device_type": (device.type or "").lower() if device else "",
        "user": label or username,
        "platform": "Spotify",
        "when": iso_minute(at),
        "account": slot,
        "song_url": song,
        "artist_url": artist_url,
        "context_url": context_url,
        "user_url": f"https://open.spotify.com/user/{urllib.parse.quote(username)}" if username else "",
        "play_url": song or PLAY_SEARCH.format(q=urllib.parse.quote(f"{title} {artist}".strip())),
    }
    return Record(fields=fields, at=at, ends_at=ends_at, track_uri=track.uri)


def shown(record: Record, now: float | None = None) -> dict[str, Any]:
    """The fields as they read at `now`: a play past its end (plus GRACE) and a pause older than PAUSED_FOR show as
    the last play."""
    now = time.time() if now is None else now
    fields = dict(record.fields)
    if fields.get("playing") and record.ends_at is not None and now > record.ends_at + GRACE:
        fields["playing"] = False
        fields["when"] = iso_minute(record.ends_at)
    if fields.get("paused") and now - record.at > PAUSED_FOR:
        fields["paused"] = False
    return fields


def last_seen(record: Record, now: float | None = None) -> float:
    """When the record's play last happened, for picking the latest one: now while it plays, else its time."""
    now = time.time() if now is None else now
    view = shown(record, now)
    if view["playing"]:
        return now
    if record.ends_at is not None and record.fields.get("playing"):
        return min(record.ends_at, now)
    return record.at
