import asyncio

import pytest

from coincoin.realtime.tools import ToolRegistry

SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


async def test_registry_exports_schema_and_dispatches_async_handler() -> None:
    registry = ToolRegistry()

    async def ping(context, arguments):
        assert context == "app"
        assert arguments == {}
        return {"ok": True, "detail": "pong"}

    registry.register("ping", "Ping test", SCHEMA, ping)
    assert registry.definitions() == [
        {
            "type": "function",
            "name": "ping",
            "description": "Ping test",
            "parameters": SCHEMA,
        }
    ]
    assert await registry.dispatch("ping", "{}", "app") == {"ok": True, "detail": "pong"}


@pytest.mark.parametrize(
    ("name", "arguments", "reason"),
    [
        ("missing", "{}", "Unknown tool"),
        ("ping", "[1]", "JSON object"),
        ("ping", "{", "valid JSON"),
    ],
)
async def test_registry_returns_explicit_invalid_call_results(name, arguments, reason) -> None:
    registry = ToolRegistry()
    registry.register("ping", "Ping test", SCHEMA, lambda *_: asyncio.sleep(0, result={"ok": True}))
    result = await registry.dispatch(name, arguments, object())
    assert result["ok"] is False
    assert reason in result["reason"]


async def test_registry_converts_handler_errors_and_timeouts() -> None:
    registry = ToolRegistry(timeout_s=0.01)

    async def fail(context, arguments):
        raise ValueError("unavailable")

    async def slow(context, arguments):
        await asyncio.sleep(1)
        return {"ok": True}

    registry.register("fail", "Fail test", SCHEMA, fail)
    registry.register("slow", "Slow test", SCHEMA, slow)
    assert (await registry.dispatch("fail", "{}", object()))["reason"] == "ValueError: unavailable"
    assert (await registry.dispatch("slow", "{}", object()))["reason"] == "Tool slow timed out"


async def test_registry_preserves_valid_refusal_results() -> None:
    registry = ToolRegistry()

    async def refuse(_context, _arguments):
        return {"ok": False, "reason": "fallen"}

    registry.register("refuse", "Refuse test", SCHEMA, refuse)
    assert await registry.dispatch("refuse", "{}", object()) == {
        "ok": False,
        "reason": "fallen",
    }
