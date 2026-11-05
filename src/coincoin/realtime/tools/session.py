"""Conversation lifecycle tools."""

from typing import Any, Protocol

from coincoin.realtime.tools import tool


class _SleepContext(Protocol):
    async def request_sleep(self) -> None: ...


@tool(
    "sleep",
    "End the current conversation and return Coin-Coin to its local wake-word listener.",
    {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
)
async def sleep(context: _SleepContext, arguments: dict[str, Any]) -> dict[str, Any]:
    await context.request_sleep()
    return {"ok": True, "detail": "Conversation ended"}
