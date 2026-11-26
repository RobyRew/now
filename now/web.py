"""The HTTP side: the public /api/now and /healthz, and /admin behind RobyRew ID.

Admin requests that change something must come from the service's own origin (Origin header) with a JSON body; the
session cookie is SameSite=Lax. A pasted session travels only in a request body, is checked with Spotify, sealed and
stored, and no response ever carries it back.
"""

from __future__ import annotations

import hmac
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import logs
from .auth import Auth
from .config import Settings
from .hub import Hub, SessionCheckFailed, SessionRefused
from .observer import describe

log = logging.getLogger("now")

SESSION_SECONDS = 12 * 3600
BODY_MAX = 8192
PROFILE_URL = "https://github.com/RobyRew"
NO_STORE = {"cache-control": "no-store"}


class SecurityHeaders:
    """Headers on every response. The admin page's script and style hashes come from its own CSP meta tag (built by
    Astro); this adds what a meta tag cannot carry."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        logto = settings.logto_endpoint
        self.admin_csp = f"frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self' {logto}"
        self.api_csp = "default-src 'none'; frame-ancestors 'none'"
        self.https = settings.public_url.startswith("https://")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        admin = scope["path"].startswith("/admin") and not scope["path"].startswith("/admin/api/")

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                extra = {
                    b"x-content-type-options": b"nosniff",
                    b"referrer-policy": b"same-origin",
                    b"x-frame-options": b"DENY",
                    b"content-security-policy": (self.admin_csp if admin else self.api_csp).encode(),
                }
                if self.https:
                    extra[b"strict-transport-security"] = b"max-age=31536000"
                present = {name.lower() for name, _ in headers}
                headers.extend((name, value) for name, value in extra.items() if name not in present)
            await send(message)

        await self.app(scope, receive, with_headers)


def create_app(settings: Settings, hub: Hub, auth: Auth, *, on_close: Callable[[], Awaitable[None]] | None = None) -> Starlette:
    """The ASGI app. `hub` is started and closed with it."""

    def admin_of(request: Request) -> dict[str, Any] | None:
        data = request.session.get("admin")
        if not isinstance(data, dict):
            return None
        if not isinstance(data.get("exp"), int) or data["exp"] < time.time():
            return None
        return data if data.get("sub") in settings.admin_subs else None

    def same_origin(request: Request) -> bool:
        return request.headers.get("origin") == settings.public_origin

    async def json_body(request: Request) -> dict[str, Any] | None:
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return None
        try:
            if int(request.headers.get("content-length", "0")) > BODY_MAX:
                return None
        except ValueError:
            return None
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > BODY_MAX:
                return None
        try:
            value = json.loads(body)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def problem(status: int, code: str, **extra: Any) -> JSONResponse:
        return JSONResponse({"error": code, **extra}, status_code=status, headers=NO_STORE)

    # ── public ──────────────────────────────────────────────────────────────

    async def home(_: Request) -> Response:
        return RedirectResponse(PROFILE_URL, status_code=302)

    async def healthz(_: Request) -> Response:
        return PlainTextResponse("ok", headers=NO_STORE)

    async def api_now(_: Request) -> Response:
        return JSONResponse(hub.now(), headers=NO_STORE)

    # ── admin: signing in and out ───────────────────────────────────────────

    async def login(request: Request) -> Response:
        if not auth.enabled:
            return problem(503, "admin_not_configured")
        try:
            return await auth.begin(request)
        except Exception as error:
            log.info("admin sign-in could not start (%s)", describe(error))
            return RedirectResponse("/admin/?signin=unavailable", status_code=303)

    async def callback(request: Request) -> Response:
        if not auth.enabled:
            return problem(503, "admin_not_configured")
        try:
            sub, name = await auth.finish(request)
        except Exception as error:
            log.info("admin sign-in failed (%s)", describe(error))
            request.session.clear()
            return RedirectResponse("/admin/?signin=failed", status_code=303)
        request.session.clear()
        if sub not in settings.admin_subs:
            log.info("admin sign-in refused: the user is not in ADMIN_SUBS")
            return RedirectResponse("/admin/?signin=refused", status_code=303)
        request.session["admin"] = {"sub": sub, "name": name, "exp": int(time.time()) + SESSION_SECONDS}
        return RedirectResponse("/admin/", status_code=303)

    async def logout(request: Request) -> Response:
        if not same_origin(request):
            return problem(403, "cross_origin")
        request.session.clear()
        return RedirectResponse("/admin/", status_code=303)

    # ── admin: the accounts ─────────────────────────────────────────────────

    async def me(request: Request) -> Response:
        if not auth.enabled:
            return problem(503, "admin_not_configured")
        admin = admin_of(request)
        if admin is None:
            return problem(401, "signin_required", signin="/admin/login")
        return JSONResponse({"name": admin.get("name", "")}, headers=NO_STORE)

    async def accounts(request: Request) -> Response:
        if admin_of(request) is None:
            return problem(401, "signin_required", signin="/admin/login")
        return JSONResponse({"accounts": hub.accounts()}, headers=NO_STORE)

    async def account(request: Request) -> Response:
        if admin_of(request) is None:
            return problem(401, "signin_required", signin="/admin/login")
        if not same_origin(request):
            return problem(403, "cross_origin")
        slot = request.path_params["slot"]
        if not 1 <= slot <= 9:
            return problem(404, "no_such_slot")
        if request.method == "DELETE":
            return Response(status_code=204, headers=NO_STORE) if await hub.remove(slot) else problem(404, "empty_slot")
        body = await json_body(request)
        if body is None:
            return problem(400, "json_body_required")
        if request.method == "PATCH":
            label = body.get("label")
            if not isinstance(label, str):
                return problem(400, "label_required")
            if not hub.rename(slot, label):
                return problem(404, "empty_slot")
            return JSONResponse(next(a for a in hub.accounts() if a["slot"] == slot), headers=NO_STORE)
        pasted = body.get("sp_dc")
        if not isinstance(pasted, str):
            return problem(400, "sp_dc_required")
        try:
            added = await hub.add(slot, pasted)
        except ValueError:
            return problem(400, "invalid_sp_dc")
        except SessionRefused:
            return problem(422, "session_refused")
        except SessionCheckFailed as error:
            return problem(502, "spotify_unreachable", detail=str(error.args[0]) if error.args else "")
        return JSONResponse(added, headers=NO_STORE)

    # ── import: copying sessions from another system, only while IMPORT_TOKEN is set ──

    async def import_account(request: Request) -> Response:
        """PUT /api/import/{slot} with `Authorization: Bearer <IMPORT_TOKEN>` and {"sp_dc": ..., "label": ...}.
        The same check and sealing as the admin page; the session never comes back in the answer."""
        if not settings.import_token:
            return problem(404, "not_found")
        given = request.headers.get("authorization", "").encode()
        if not hmac.compare_digest(given, f"Bearer {settings.import_token}".encode()):
            log.info("import refused: wrong or missing token")
            return problem(401, "unauthorized")
        slot = request.path_params["slot"]
        if not 1 <= slot <= 9:
            return problem(404, "no_such_slot")
        body = await json_body(request)
        if body is None:
            return problem(400, "json_body_required")
        pasted, label = body.get("sp_dc"), body.get("label")
        if not isinstance(pasted, str):
            return problem(400, "sp_dc_required")
        try:
            added = await hub.add(slot, pasted)
        except ValueError:
            return problem(400, "invalid_sp_dc")
        except SessionRefused:
            return problem(422, "session_refused")
        except SessionCheckFailed as error:
            return problem(502, "spotify_unreachable", detail=str(error.args[0]) if error.args else "")
        if isinstance(label, str) and label.strip():
            hub.rename(slot, label)
            added = next(a for a in hub.accounts() if a["slot"] == slot)
        log.info("slot %d: imported", slot)
        return JSONResponse(added, headers=NO_STORE)

    async def failed(_: Request, error: Exception) -> Response:
        log.warning("request failed (%s)", describe(error))
        return problem(500, "internal")

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        import asyncio

        logs.install_loop_handler(asyncio.get_running_loop())
        await hub.start()
        try:
            yield
        finally:
            await hub.close()
            if on_close is not None:
                await on_close()

    routes = [
        Route("/", home),
        Route("/healthz", healthz),
        Route("/api/now", api_now),
        Route("/api/import/{slot:int}", import_account, methods=["PUT"]),
        Route("/admin", lambda _: RedirectResponse("/admin/", status_code=308)),
        Route("/admin/login", login),
        Route("/admin/callback", callback),
        Route("/admin/logout", logout, methods=["POST"]),
        Route("/admin/api/me", me),
        Route("/admin/api/accounts", accounts),
        Route("/admin/api/accounts/{slot:int}", account, methods=["PUT", "PATCH", "DELETE"]),
        Mount("/admin", StaticFiles(directory=settings.static_dir, html=True, check_dir=False), name="admin"),
    ]
    middleware = [
        Middleware(SecurityHeaders, settings=settings),
        Middleware(
            SessionMiddleware,
            secret_key=settings.session_secret,
            session_cookie="__Host-now",
            max_age=SESSION_SECONDS,
            path="/",
            same_site="lax",
            https_only=True,
        ),
    ]
    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan, exception_handlers={Exception: failed})
