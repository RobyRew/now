"""Signing in to /admin with RobyRew ID (Logto, OpenID Connect).

Authlib runs the authorization-code flow with PKCE (S256), state and nonce, and checks the ID token (signature from
Logto's JWKS, issuer, audience, expiry, nonce). The Logto app is a Traditional Web app, first-party, with the
redirect URI <PUBLIC_URL>/admin/callback. Only Logto user ids listed in ADMIN_SUBS get an admin session.
"""

from __future__ import annotations

from typing import Any

from authlib.integrations.starlette_client import OAuth
from starlette.requests import Request
from starlette.responses import Response

from .config import Settings


class Auth:
    def __init__(self, settings: Settings, *, client_kwargs: dict[str, Any] | None = None) -> None:
        self.settings = settings
        self.enabled = settings.admin_enabled
        self._client: Any = None
        if self.enabled:
            oauth = OAuth()
            oauth.register(
                name="logto",
                client_id=settings.logto_app_id,
                client_secret=settings.logto_app_secret,
                server_metadata_url=f"{settings.logto_endpoint}/oidc/.well-known/openid-configuration",
                client_kwargs={"scope": "openid profile", "code_challenge_method": "S256", **(client_kwargs or {})},
            )
            self._client = oauth.create_client("logto")

    @property
    def redirect_uri(self) -> str:
        return f"{self.settings.public_url}/admin/callback"

    async def begin(self, request: Request) -> Response:
        """The redirect to Logto's sign-in page. State, nonce and the PKCE verifier go into the session cookie."""
        return await self._client.authorize_redirect(request, self.redirect_uri)

    async def finish(self, request: Request) -> tuple[str, str]:
        """The signed-in user's Logto id and username, from the checked ID token. Raises on any failure."""
        token = await self._client.authorize_access_token(request)
        claims = token.get("userinfo") or {}
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise ValueError("the ID token has no subject")
        name = claims.get("username") or claims.get("name") or ""
        return sub, str(name)
