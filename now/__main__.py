"""python -m now: the service, on one uvicorn worker (the observers live in this process)."""

from __future__ import annotations

import asyncio
import sys

import uvicorn

from . import logs
from .auth import Auth
from .config import ConfigError, Settings
from .crypto import Sealer
from .hub import Hub
from .store import Store
from .web import create_app


def main() -> None:
    logs.setup()
    try:
        settings = Settings.from_env()
    except ConfigError as error:
        sys.exit(f"now: {error}")
    store = Store(settings.data_dir / "now.db")
    hub = Hub(store, Sealer(settings.key, settings.previous_key))

    async def closed() -> None:
        await asyncio.to_thread(store.close)

    app = create_app(settings, hub, Auth(settings), on_close=closed)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        proxy_headers=False,  # redirects use PUBLIC_URL, never the request's own scheme or host
        server_header=False,
        workers=1,
    )


if __name__ == "__main__":
    main()
