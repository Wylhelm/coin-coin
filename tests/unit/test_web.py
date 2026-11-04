import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from coincoin.app import main
from coincoin.web.server import create_app


async def test_hello_and_healthz() -> None:
    async with TestClient(TestServer(create_app())) as client:
        health = await client.get("/healthz")
        assert health.status == 200
        assert await health.json() == {"status": "ok"}
        page = await client.get("/")
        assert page.status == 200
        assert page.content_type == "text/html"
        assert "Coin-Coin" in await page.text()
        assert (await client.get("/static/app.js")).status == 200
        missing = await client.get("/missing")
        assert missing.status == 404


def test_entrypoint_is_offline_and_uses_fixed_internal_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ECHO", raising=False)
    called: list[tuple[str, int]] = []

    def run_app(app: web.Application, *, host: str, port: int) -> None:
        assert isinstance(app, web.Application)
        called.append((host, port))

    monkeypatch.setattr(web, "run_app", run_app)
    main([])
    assert called == [("0.0.0.0", 8765)]
    entry = json.loads(next((tmp_path / "logs").glob("*.jsonl")).read_text(encoding="utf-8"))
    assert entry["type"] == "state"
    assert entry["service"] == "starting"


def test_entrypoint_honors_echo_environment_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ECHO", "1")
    echo_modes: list[bool] = []

    def fake_create_app(**options: object) -> web.Application:
        echo = options.get("echo")
        assert isinstance(echo, bool)
        echo_modes.append(echo)
        return web.Application()

    monkeypatch.setattr("coincoin.app.create_app", fake_create_app)
    monkeypatch.setattr(web, "run_app", lambda app, *, host, port: None)

    main([])

    assert echo_modes == [True]
