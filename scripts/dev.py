"""A local run with a fake Spotify and a fake Logto:  .venv.nosync/bin/python scripts/dev.py

Not part of the image. The service itself runs unchanged (now.web.create_app) on http://localhost:4350; only its two
outside parties are replaced:
- Spotify: any plausible sp_dc signs in, and each account plays a scripted song. Slots 1 and 2 start filled.
- Logto: a small OpenID provider on http://localhost:4351 whose sign-in page is one button.
Build the admin page first (cd admin && npm run build). Data goes to a fresh temporary folder on every run.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import sys
import tempfile
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("AUTHLIB_INSECURE_TRANSPORT", "1")  # the fake provider is plain http on localhost

import uvicorn  # noqa: E402
from spotapi.models.playback import Device, PlayerState  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response  # noqa: E402
from starlette.routing import Route  # noqa: E402

from now import logs  # noqa: E402
from now.auth import Auth  # noqa: E402
from now.config import Settings  # noqa: E402
from now.crypto import Sealer  # noqa: E402
from now.hub import Hub  # noqa: E402
from now.store import Store  # noqa: E402
from now.web import create_app  # noqa: E402
from tests.fakes import FakeClient, FakeLogto, FakeObserver, cluster  # noqa: E402

SERVICE, PROVIDER = 4350, 4351
SONGS = [
    ("Midnight City", "M83", "Deep Focus", True),
    ("Teardrop", "Massive Attack", "Trip Hop Classics", False),
    ("Nightcall", "Kavinsky", "Night Drive", True),
    ("Intro", "The xx", "Late Night Lofi", False),
]


class DemoObserver(FakeObserver):
    """Plays one scripted song per account: playing, or paused."""

    async def start(self) -> None:
        self.started.set()
        await self.emit("ready")
        title, artist, playlist, playing = SONGS[sum(map(ord, self.client.cookie)) % len(SONGS)]
        raw = cluster(title=title, artist=artist, playing=playing, paused=not playing, description=playlist)
        self.state = PlayerState.from_cluster(raw)
        self.devices = tuple(Device(i, v["name"], v["device_type"]) for i, v in raw["devices"].items())
        self.extras = {"context_description": playlist}
        await self.emit("state_update", None, self.state)
        await self.stopped.wait()


async def demo_sign_in(cookie: str) -> FakeClient:
    return FakeClient(cookie)


async def demo_username(client: FakeClient) -> str:
    return "demo_" + client.cookie[-4:].lower()


def provider_app(fake: FakeLogto) -> Starlette:
    async def discovery(_: Request) -> Response:
        return JSONResponse(fake.discovery())

    async def jwks(_: Request) -> Response:
        return JSONResponse(fake.jwks())

    async def sign_in_page(request: Request) -> Response:
        approve = f"/oidc/approve?{request.url.query}"
        return HTMLResponse(
            "<!doctype html><meta name=viewport content='width=device-width'><title>Fake Logto</title>"
            "<body style='font:16px system-ui;display:grid;place-items:center;min-height:90vh'>"
            "<main style='text-align:center'><p>Fake Logto (local test only)</p>"
            f"<p><a href='{approve}' style='padding:10px 18px;background:#2563eb;color:#fff;border-radius:10px;"
            f"text-decoration:none'>Continue as @{fake.username}</a></p></main>"
        )

    async def approve(request: Request) -> Response:
        code, state = fake.authorize(str(request.url))
        redirect = dict(request.query_params)["redirect_uri"]
        return RedirectResponse(f"{redirect}?code={code}&state={state}", status_code=302)

    async def token(request: Request) -> Response:
        # Parsed here: Starlette's request.form() needs python-multipart, which the service does not install.
        form = dict(urllib.parse.parse_qsl((await request.body()).decode()))
        status, body = fake.token(form, request.headers.get("authorization", ""))
        return JSONResponse(body, status_code=status)

    return Starlette(routes=[
        Route("/oidc/.well-known/openid-configuration", discovery),
        Route("/oidc/jwks", jwks),
        Route("/oidc/auth", sign_in_page),
        Route("/oidc/approve", approve),
        Route("/oidc/token", token, methods=["POST"]),
    ])


async def main() -> None:
    logs.setup()
    data = Path(tempfile.mkdtemp(prefix="now-dev-"))
    settings = Settings(
        public_url=f"http://localhost:{SERVICE}",
        data_dir=data,
        key=os.urandom(32),
        previous_key=None,
        session_secret=secrets.token_urlsafe(48),
        logto_endpoint=f"http://localhost:{PROVIDER}",
        logto_app_id="dev-app",
        logto_app_secret="dev-secret",
        admin_subs=frozenset({"w2nbkvwmzzyp"}),
        static_dir=ROOT / "admin" / "dist",
    )
    store = Store(data / "now.db")
    hub = Hub(store, Sealer(settings.key), sign_in=demo_sign_in, username_of=demo_username,
              watcher_options={"observer_cls": DemoObserver})
    fake = FakeLogto("dev-app", "dev-secret", base=f"http://localhost:{PROVIDER}")
    service = uvicorn.Server(uvicorn.Config(create_app(settings, hub, Auth(settings)), port=SERVICE, log_config=None,
                                            access_log=False))
    provider = uvicorn.Server(uvicorn.Config(provider_app(fake), port=PROVIDER, log_config=None, access_log=False))

    async def seed() -> None:
        while not service.started:
            await asyncio.sleep(0.05)
        await hub.add(1, "AQ" + base64.urlsafe_b64encode(b"first demo account x").decode().rstrip("="))
        await hub.add(2, "AQ" + base64.urlsafe_b64encode(b"second demo account").decode().rstrip("="))
        hub.rename(1, "Cosmin")
        print(f"\nnow: http://localhost:{SERVICE}/admin/  (fake Spotify, fake Logto; data in {data})\n", flush=True)

    await asyncio.gather(service.serve(), provider.serve(), seed())


if __name__ == "__main__":
    asyncio.run(main())
