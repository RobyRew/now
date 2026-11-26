"""Settings, read once from the environment at start-up."""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """A missing or malformed setting. The message names the variable, never its value."""


def _secret_key(name: str, *, required: bool) -> bytes | None:
    """A 32-byte key given as base64 (`openssl rand -base64 32`)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        if required:
            raise ConfigError(f"{name} is not set: 32 random bytes as base64 (openssl rand -base64 32)")
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise ConfigError(f"{name} is not valid base64") from None
    if len(key) != 32:
        raise ConfigError(f"{name} must decode to exactly 32 bytes")
    return key


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the service reads from its environment."""

    public_url: str
    """The address people use, without a trailing slash: redirect URIs and the CSRF origin check derive from it."""
    data_dir: Path
    """The named volume: the SQLite file lives here."""
    key: bytes
    """NOW_KEY: seals the Spotify sessions at rest."""
    previous_key: bytes | None
    """NOW_KEY_PREVIOUS: still opens sessions sealed before a key change; they are sealed again with NOW_KEY."""
    session_secret: str
    """Signs the admin session cookie."""
    logto_endpoint: str
    logto_app_id: str
    logto_app_secret: str
    admin_subs: frozenset[str]
    """Logto user ids allowed into /admin."""
    static_dir: Path
    """The built admin page."""
    host: str = "0.0.0.0"
    port: int = 8080

    @property
    def admin_enabled(self) -> bool:
        """The admin page needs a Logto app and at least one allowed user."""
        return bool(self.logto_app_id and self.logto_app_secret and self.admin_subs)

    @property
    def public_origin(self) -> str:
        """Scheme and host of public_url, as a browser sends it in an Origin header."""
        scheme, _, rest = self.public_url.partition("://")
        return f"{scheme}://{rest.split('/', 1)[0]}"

    @classmethod
    def from_env(cls) -> Settings:
        public_url = os.environ.get("PUBLIC_URL", "https://now.spotify.robyrew.com").strip().rstrip("/")
        if not public_url.startswith(("https://", "http://")):
            raise ConfigError("PUBLIC_URL must start with https:// (or http:// for a local run)")
        session_secret = os.environ.get("SESSION_SECRET", "").strip()
        if len(session_secret) < 32:
            raise ConfigError("SESSION_SECRET must be at least 32 characters (openssl rand -base64 48)")
        key = _secret_key("NOW_KEY", required=True)
        assert key is not None
        subs = frozenset(s.strip() for s in os.environ.get("ADMIN_SUBS", "").split(",") if s.strip())
        try:
            port = int(os.environ.get("PORT", "8080"))
        except ValueError:
            raise ConfigError("PORT must be a number") from None
        default_static = Path(__file__).resolve().parent.parent / "admin" / "dist"
        return cls(
            public_url=public_url,
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            key=key,
            previous_key=_secret_key("NOW_KEY_PREVIOUS", required=False),
            session_secret=session_secret,
            logto_endpoint=os.environ.get("LOGTO_ENDPOINT", "https://auth.robyrew.com").strip().rstrip("/"),
            logto_app_id=os.environ.get("LOGTO_APP_ID", "").strip(),
            logto_app_secret=os.environ.get("LOGTO_APP_SECRET", "").strip(),
            admin_subs=subs,
            static_dir=Path(os.environ.get("STATIC_DIR", str(default_static))),
            host=os.environ.get("HOST", "0.0.0.0"),
            port=port,
        )
