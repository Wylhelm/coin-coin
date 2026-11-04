"""Minimal hello page and credential-free liveness endpoint."""

from pathlib import Path

from aiohttp import web

_INDEX = Path(__file__).parent / "static" / "index.html"


async def _index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(_INDEX)


async def _healthz(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def create_app() -> web.Application:
    """Create the S0 HTTP application without opening external connections."""
    app = web.Application()
    app.add_routes([web.get("/", _index), web.get("/healthz", _healthz)])
    return app
