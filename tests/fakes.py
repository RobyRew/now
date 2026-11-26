"""Stand-ins for Spotify and Logto, so the tests never reach either."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import urllib.parse
from typing import Any

import httpx2
from joserfc import jwt
from joserfc.jwk import ECKey
from spotapi import LoginExpired
from spotapi.types import HTTPError

GOOD = "AQ" + "g" * 40  # a plausible sp_dc value
EXPIRED = "AQ" + "x" * 40
FLAKY = "AQ" + "f" * 40
LEAKY = "AQ" + "l" * 40  # sign-in fails with an error whose message contains the cookie


def cluster(*, playing: bool = True, paused: bool = False, title: str = "Midnight City", artist: str = "M83",
            stamp_ms: int | None = None, device: str | None = "dev1", track: str = "spotify:track:abc123",
            position: int = 30_000, duration: int = 240_000, context: str = "spotify:playlist:37i9dQZF1DX",
            description: str = "Deep Focus") -> dict[str, Any]:
    """A Connect cluster shaped like the dealer's JSON (timestamps as strings, as Spotify sends them)."""
    stamp_ms = int(time.time() * 1000) if stamp_ms is None else stamp_ms
    return {
        "timestamp": str(stamp_ms),
        "active_device_id": device or "",
        "player_state": {
            "timestamp": str(stamp_ms),
            "context_uri": context,
            "context_metadata": {"context_description": description},
            "is_playing": playing,
            "is_paused": paused,
            "position_as_of_timestamp": str(position),
            "duration": str(duration),
            "playback_speed": 1.0,
            "track": {
                "uri": track,
                "uid": "u1",
                "provider": "context",
                "metadata": {
                    "title": title,
                    "artist_name": artist,
                    "album_title": "Hurry Up, We're Dreaming",
                    "image_url": "https://i.scdn.co/image/cover",
                    "artist_uri": "spotify:artist:63MQldklfxkjYDoUE4Tppz",
                    "duration": str(duration),
                },
            },
        },
        "devices": {
            "dev1": {"name": "Cosmin's iPhone", "device_type": "SMARTPHONE", "volume": 65535, "can_play": True,
                     "capabilities": {"is_controllable": True}},
        },
    }


def idle_cluster(stamp_ms: int | None = None) -> dict[str, Any]:
    stamp_ms = int(time.time() * 1000) if stamp_ms is None else stamp_ms
    return {"timestamp": str(stamp_ms), "devices": {}}


class FakeClient:
    """Enough of SpotAPI's AsyncClient for the code under test: an async context manager with a session."""

    instances: list[FakeClient] = []

    def __init__(self, cookie: str) -> None:
        self.cookie = cookie
        self.closed = False
        self.client = self  # PlayerStatus reads session.client
        self.session = self
        FakeClient.instances.append(self)

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        self.closed = True


async def fake_sign_in(cookie: str) -> FakeClient:
    value = cookie.removeprefix("sp_dc=")
    if value == EXPIRED:
        raise LoginExpired("Spotify rejected the session", status_code=401)
    if value == FLAKY:
        raise HTTPError("upstream", status_code=503)
    if value == LEAKY:
        raise RuntimeError(f"request failed for cookie {cookie}")
    return FakeClient(cookie)


async def fake_username(client: FakeClient) -> str:
    return "cosmin" if client.cookie.endswith(GOOD) else "someone-else"


class FakeObserver:
    """The interface Watcher uses on observer.Observer, driven by the test instead of a dealer socket."""

    instances: list[FakeObserver] = []

    def __init__(self, client: Any, *, name: str) -> None:
        self.client, self.name, self.session = client, name, client
        self.state: Any = None
        self.devices: tuple[Any, ...] = ()
        self.extras: dict[str, str] = {}
        self.listeners: dict[str, list[Any]] = {}
        self.stopped = asyncio.Event()
        self.started = asyncio.Event()
        self.fail_with: BaseException | None = None
        FakeObserver.instances.append(self)

    def add_listener(self, callback: Any, name: str) -> None:
        self.listeners.setdefault(name, []).append(callback)

    async def emit(self, name: str, *args: Any) -> None:
        await asyncio.gather(*(cb(*args) for cb in self.listeners.get(name, [])))

    async def start(self) -> None:
        self.started.set()
        if self.fail_with is not None:
            raise self.fail_with
        await self.emit("ready")
        await self.stopped.wait()

    async def close(self) -> None:
        self.stopped.set()


async def until(condition: Any, timeout: float = 2.0) -> None:
    """Wait until condition() is true, letting the loop run."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


# ── a fake Logto ────────────────────────────────────────────────────────────

class FakeLogto:
    """Discovery, JWKS and the token endpoint of an OIDC provider: behind an httpx2 MockTransport in the tests, and
    over real HTTP in scripts/dev.py."""

    def __init__(self, client_id: str, client_secret: str, sub: str = "w2nbkvwmzzyp", username: str = "robyrew",
                 base: str = "https://auth.test") -> None:
        self.client_id, self.client_secret = client_id, client_secret
        self.sub, self.username = sub, username
        self.base = base.rstrip("/")
        self.issuer = f"{self.base}/oidc"
        self.key = ECKey.generate_key("P-384", parameters={"kid": "test-key", "alg": "ES384", "use": "sig"})
        self.codes: dict[str, dict[str, str]] = {}
        self.nonce_override: str | None = None
        self.token_requests: list[dict[str, str]] = []

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def authorize(self, location: str) -> tuple[str, str]:
        """What Logto's sign-in page does: remember the request and hand back (code, state)."""
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))
        assert query["client_id"] == self.client_id
        assert query["code_challenge_method"] == "S256"
        code = f"code-{len(self.codes)}-{int(time.time() * 1000)}"
        self.codes[code] = query
        return code, query["state"]

    def discovery(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/auth",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/jwks",
            "end_session_endpoint": f"{self.issuer}/session/end",
            "response_types_supported": ["code"],
            # As auth.robyrew.com/oidc/.well-known/openid-configuration lists them.
            "id_token_signing_alg_values_supported": ["ES384"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            "code_challenge_methods_supported": ["S256"],
        }

    def jwks(self) -> dict[str, Any]:
        return {"keys": [self.key.as_dict(private=False)]}

    def token(self, form: dict[str, str], authorization: str) -> tuple[int, dict[str, Any]]:
        self.token_requests.append(form)
        expected = "Basic " + base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        pending = self.codes.pop(form.get("code", ""), None)
        if authorization != expected or pending is None:
            return 400, {"error": "invalid_grant"}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
        if challenge != pending["code_challenge"] or form.get("redirect_uri") != pending["redirect_uri"]:
            return 400, {"error": "invalid_grant"}
        now = int(time.time())
        claims = {"iss": self.issuer, "aud": self.client_id, "sub": self.sub, "username": self.username,
                  "iat": now, "exp": now + 300, "nonce": self.nonce_override or pending.get("nonce", "")}
        # ES384, as auth.robyrew.com signs (its JWKS holds one EC P-384 key). joserfc only signs it when asked.
        id_token = jwt.encode({"alg": "ES384", "kid": "test-key"}, claims, self.key, algorithms=["ES384"])
        return 200, {"access_token": "opaque", "token_type": "Bearer", "expires_in": 3600, "id_token": id_token,
                     "scope": "openid profile"}

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx2.Response(200, json=self.discovery())
        if path.endswith("/jwks"):
            return httpx2.Response(200, json=self.jwks())
        if path.endswith("/token"):
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            status, body = self.token(form, request.headers.get("authorization", ""))
            return httpx2.Response(status, json=body)
        return httpx2.Response(404, content=json.dumps({"error": "not_found"}).encode())
