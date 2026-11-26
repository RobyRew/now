"""Logging that cannot leak a session.

The service's own lines carry a slot, an event, an error type and an HTTP status, and nothing else. Third-party
libraries are kept quiet: SpotAPI logs a failed token refresh with the exception attached and its URLs carry tokens,
so its loggers are switched off entirely; and no log line anywhere prints a traceback, because exception messages
are where a cookie or a token would show up.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from .observer import describe


class _NoTracebacks(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def setup(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_NoTracebacks())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    spotapi = logging.getLogger("spotapi")
    spotapi.handlers[:] = [logging.NullHandler()]
    spotapi.propagate = False
    spotapi.setLevel(logging.CRITICAL + 1)
    for name in ("httpx", "httpx2", "httpcore", "httpcore2", "authlib", "wreq"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").disabled = True


def install_loop_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Unhandled task failures are logged by type only."""

    def handle(_: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        error = context.get("exception")
        what = describe(error) if isinstance(error, BaseException) else "no exception"
        logging.getLogger("now").warning("background task failed (%s)", what)

    loop.set_exception_handler(handle)
