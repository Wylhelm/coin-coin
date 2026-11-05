"""Scriptable loopback OpenClaw-compatible HTTP endpoint for tests."""

import asyncio
from collections.abc import Mapping
from typing import Any

from aiohttp import web


class FakeOpenClaw:
    """Serve deterministic OpenClaw responses without external network access."""

    def __init__(
        self,
        *,
        status: int = 200,
        statuses: list[int] | None = None,
        content: str = "Souvenir de test.",
        delay_s: float = 0,
        expected_token: str | None = None,
        prompt_delays: Mapping[str, float] | None = None,
    ) -> None:
        self.status = status
        self.statuses = statuses or []
        self._status_index = 0
        self.content = content
        self.delay_s = delay_s
        self.expected_token = expected_token
        self.prompt_delays = dict(prompt_delays or {})
        self.memories: list[str] = []
        self.requests: list[dict[str, Any]] = []
        self.app = web.Application()
        self.app.router.add_post("/v1/chat/completions", self._chat_completions)

    async def _chat_completions(self, request: web.Request) -> web.Response:
        if self.expected_token is not None and request.headers.get("Authorization") != (
            f"Bearer {self.expected_token}"
        ):
            return web.json_response({"error": {"message": "unauthorized"}}, status=401)
        body = await request.json()
        if not isinstance(body, Mapping):
            return web.json_response({"error": {"message": "invalid request"}}, status=400)
        self.requests.append({"body": dict(body)})
        prompt = _prompt(body)
        delay_s = self.delay_s + sum(
            delay for prefix, delay in self.prompt_delays.items() if prompt.startswith(prefix)
        )
        if delay_s:
            await asyncio.sleep(delay_s)
        status = (
            self.statuses[self._status_index]
            if self._status_index < len(self.statuses)
            else self.status
        )
        self._status_index += 1
        if status != 200:
            return web.json_response(
                {"error": {"message": "scripted fake response"}}, status=status
            )
        content = self.content
        if prompt.startswith("[À mémoriser] "):
            self.memories.append(prompt.removeprefix("[À mémoriser] "))
        elif prompt.startswith("[Coin-Coin se réveille]") and self.memories:
            content = "\n".join(self.memories)
        return web.json_response(
            {"choices": [{"message": {"role": "assistant", "content": content}}]}
        )


def _prompt(body: Mapping[str, Any]) -> str:
    messages = body.get("messages")
    if isinstance(messages, list) and messages and isinstance(messages[0], Mapping):
        content = messages[0].get("content")
        if isinstance(content, str):
            return content
    return ""
