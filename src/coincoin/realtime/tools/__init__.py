"""Typed Realtime tool registry and bounded asynchronous dispatch."""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)
_SECRET = re.compile(r"sk-[A-Za-z0-9_-]+|(?i:Bearer)\s+[^\s,;]+")


ToolHandler = Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    schema: dict[str, Any]
    handler: ToolHandler
    manages_timeout: bool = False


_DEFAULT_TOOLS: dict[str, ToolDefinition] = {}


def tool(
    name: str, description: str, schema: dict[str, Any]
) -> Callable[[ToolHandler], ToolHandler]:
    """Register a default tool definition and return its handler unchanged."""
    if not name or not description or schema.get("type") != "object":
        raise ValueError("Tools require a name, description, and object JSON schema")

    def register(handler: ToolHandler) -> ToolHandler:
        if name in _DEFAULT_TOOLS:
            raise ValueError(f"Tool already registered: {name}")
        _DEFAULT_TOOLS[name] = ToolDefinition(name, description, schema, handler)
        return handler

    return register


class ToolRegistry:
    """Dispatch tools with a deadline unless a handler manages its own bounded execution."""

    def __init__(self, timeout_s: float = 10) -> None:
        if timeout_s <= 0:
            raise ValueError("Tool timeout must be positive")
        self.timeout_s = timeout_s
        self._tools: dict[str, ToolDefinition] = {}

    def register(
        self,
        name: str,
        description: str,
        schema: dict[str, Any],
        handler: ToolHandler,
        *,
        manages_timeout: bool = False,
    ) -> None:
        if not name or not description or schema.get("type") != "object":
            raise ValueError("Tools require a name, description, and object JSON schema")
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        self._tools[name] = ToolDefinition(name, description, schema, handler, manages_timeout)

    def definitions(self) -> list[dict[str, object]]:
        return [
            {
                "type": "function",
                "name": definition.name,
                "description": definition.description,
                "parameters": definition.schema,
            }
            for definition in self._tools.values()
        ]

    async def dispatch(self, name: str, arguments_json: str, context: Any) -> dict[str, Any]:
        definition = self._tools.get(name)
        if definition is None:
            return {"ok": False, "reason": f"Unknown tool: {name}"}
        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError:
            return {"ok": False, "reason": "Tool arguments are not valid JSON"}
        if not isinstance(arguments, dict):
            return {"ok": False, "reason": "Tool arguments must be a JSON object"}
        try:
            timeout_s = None if definition.manages_timeout else self.timeout_s
            async with asyncio.timeout(timeout_s):
                result = await definition.handler(context, arguments)
        except TimeoutError:
            _LOGGER.warning("Realtime tool %s exceeded its %.1f s deadline", name, self.timeout_s)
            return {"ok": False, "reason": f"Tool {name} timed out"}
        except Exception as exc:
            detail = _SECRET.sub("[REDACTED]", str(exc))
            _LOGGER.error("Realtime tool %s failed: %s", name, type(exc).__name__)
            return {"ok": False, "reason": f"{type(exc).__name__}: {detail}"}
        if not isinstance(result, dict):
            return {"ok": False, "reason": "Tool returned an invalid result"}
        if result.get("ok") is True:
            return result
        if result.get("ok") is False and isinstance(result.get("reason"), str):
            return result
        return {"ok": False, "reason": "Tool returned an invalid result"}


def default_tools() -> tuple[ToolDefinition, ...]:
    """Return registered built-ins in registration order."""
    from coincoin.realtime.tools import session as _session_tools

    _ = _session_tools
    return tuple(_DEFAULT_TOOLS.values())
