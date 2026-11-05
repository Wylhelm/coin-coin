"""Fixture-backed fake for the actual GA Realtime server event sequence."""

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer


class FakeRealtimeServer:
    """Replay the recorded session/tool/audio/interruption events over WebSocket."""

    def __init__(self, fixture: Path | None = None) -> None:
        path = fixture or Path(__file__).parent / "fixtures" / "realtime_trace.jsonl"
        self.events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        completed_response = next(
            index
            for index, event in enumerate(self.events)
            if event.get("type") == "response.done"
            and event.get("response", {}).get("status") == "completed"
        )
        self._input_events = self.events[2 : completed_response + 1]
        self._followup_events = self.events[completed_response + 1 :]
        self.received: list[dict[str, Any]] = []
        self.audio_input = bytearray()
        self.truncated = asyncio.Event()
        self._did_replay_input = False
        self._did_replay_followup = False
        app = web.Application()
        app.router.add_get("/v1/realtime", self._websocket)
        self.server = TestServer(app)

    @property
    def websocket_url(self) -> str:
        return str(self.server.make_url("/v1/realtime")).replace("http://", "ws://", 1)

    async def start(self) -> None:
        await self.server.start_server()

    async def close(self) -> None:
        await self.server.close()

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        await websocket.send_json(self.events[0])
        async for message in websocket:
            if message.type != WSMsgType.TEXT:
                continue
            event = json.loads(message.data)
            self.received.append(event)
            kind = event.get("type")
            if kind == "session.update":
                await websocket.send_json(self.events[1])
            elif kind == "input_audio_buffer.append":
                self.audio_input.extend(base64.b64decode(event["audio"]))
                if not self._did_replay_input:
                    self._did_replay_input = True
                    for server_event in self._input_events:
                        await websocket.send_json(server_event)
            elif kind == "conversation.item.truncate":
                self.truncated.set()
            elif kind == "response.create" and not self._did_replay_followup:
                self._did_replay_followup = True
                for server_event in self._followup_events:
                    await websocket.send_json(server_event)
        return websocket
