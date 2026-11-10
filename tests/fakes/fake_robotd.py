"""Scriptable robot model, usable directly or behind a TCP/Unix JSON-RPC server."""

import asyncio
import contextlib
import copy
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coincoin.clock import Clock, SystemClock
from coincoin.robot.frames import FrameError
from coincoin.robot.jsonrpc import JsonRpcError
from coincoin.robot.link import Frame, LinkDown, RobotRefused


@dataclass(frozen=True)
class Command:
    t: float
    method: str
    params: dict[str, Any] | None
    notification: bool


class FakeRobotLink:
    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock if clock is not None else SystemClock()
        self.connected = True
        self.streaming = True
        self.have_state = True
        self.records: list[Command] = []
        self.refusals: dict[str, str] = {}
        self.responses: dict[str, Any] = {}
        self.fail_notify: str | None = None
        self.auto_home = True
        self.mode = "walk"
        self.remote_mode_switch = True
        self.policies: dict[str, Any] = {"enabled": True, "homed": True, "sitting": False}
        self.safety = {"fallen": False, "picked_up": False}
        self.yaw = 0.0
        self.position = [0.0, 0.0, 0.12]
        self.head = [0.0, 0.0, 0.0, 0.0]
        self.twist = [0.0, 0.0, 0.0]
        self._last_tick = self.clock.now()
        self._last_state = self.clock.now()

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    def _integrate(self) -> None:
        now = self.clock.now()
        elapsed = now - self._last_tick
        self._last_tick = now
        self.position[0] += self.twist[0] * elapsed * math.cos(self.yaw)
        self.position[1] += self.twist[0] * elapsed * math.sin(self.yaw)
        self.yaw += self.twist[2] * elapsed

    def state(self) -> dict[str, Any] | None:
        if not self.have_state:
            return None
        self._integrate()
        if self.streaming:
            self._last_state = self.clock.now()
        return copy.deepcopy(
            {
                "safety": self.safety,
                "odom": {
                    "yaw": math.atan2(math.sin(self.yaw), math.cos(self.yaw)),
                    "position": self.position,
                },
                "head": self.head,
                "move": {"requested": self.twist, "applied": self.twist, "limited_by": []},
            }
        )

    def state_age(self) -> float:
        return self.clock.now() - self._last_state if self.have_state else math.inf

    async def frame(self) -> Frame:
        raise FrameError("No fake camera configured")

    def _record(self, method: str, params: dict[str, Any] | None, notification: bool) -> None:
        if not self.connected:
            raise LinkDown("fake link down")
        self._integrate()
        self.records.append(Command(self.clock.now(), method, copy.deepcopy(params), notification))
        if method in self.refusals:
            raise RobotRefused(self.refusals[method])

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 2.0,  # noqa: ASYNC109 - RobotLink contract
    ) -> Any:
        self._record(method, params, False)
        if method in self.responses:
            return copy.deepcopy(self.responses[method])
        match method:
            case "hello":
                return {"api_version": 47}
            case "robot.subscribe":
                return {"accepted": True}
            case "robot.mode":
                return {"mode": self.mode}
            case "robot.setMode":
                assert params is not None
                if not self.remote_mode_switch:
                    # mediad's WebRTC route refuses the method before robotd sees it.
                    raise JsonRpcError(-32601, "robot.setMode is not available over WebRTC")
                if params["mode"] not in ("walk", "roller"):
                    raise RobotRefused('mode must be "walk" or "roller"')
                self.mode = params["mode"]
            case "robot.policies":
                return copy.deepcopy(self.policies)
            case "robot.enable":
                self.policies["enabled"] = True
                self.policies["homed"] = self.auto_home
            case "robot.skills":
                return {
                    "skills": [{"name": "roulade"}, {"name": "kick_left"}, {"name": "kick_right"}],
                    "built_in": ["ground_pick", "sit_toggle"],
                }
            case "robot.look":
                params = params or {}
                x, y, z = params.get("x", 1), params.get("y", 0), params.get("z", 0)
                return {
                    "head": {
                        "neck_pitch": 0.0,
                        "head_pitch": math.atan2(z, math.hypot(x, y)),
                        "head_yaw": math.atan2(y, x),
                        "head_roll": 0.0,
                    },
                    "clamped": False,
                }
            case "robot.do":
                if params is not None and params.get("skill") == "sit_toggle":
                    self.policies["sitting"] = not self.policies["sitting"]
            case "robot.stop":
                self.twist = [0.0, 0.0, 0.0]
            case "robot.health":
                return {"healthy": True}
        return {"accepted": True}

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self.fail_notify == method:
            self.connected = False
            raise LinkDown("scripted notification failure")
        self._record(method, params, True)
        params = params or {}
        if method == "robot.move":
            self.twist = [params["vx"], params["vy"], params["vyaw"]]
        elif method == "robot.head":
            self.head = [
                params[key] for key in ("neck_pitch", "head_pitch", "head_yaw", "head_roll")
            ]


class FakeRobotd(FakeRobotLink):
    def __init__(self) -> None:
        super().__init__()
        self.server: asyncio.Server | None = None
        self.port = 0
        self.clients: set[asyncio.StreamWriter] = set()
        self.handlers: set[asyncio.Task[None]] = set()
        self.push_states = True

    async def start(self, *, port: int = 0, unix: Path | None = None) -> None:
        if unix is None:
            self.server = await asyncio.start_server(self._handle, "127.0.0.1", port)
            self.port = self.server.sockets[0].getsockname()[1]
        else:
            self.server = await asyncio.start_unix_server(self._handle, unix)

    async def disconnect_clients(self) -> None:
        for writer in list(self.clients):
            writer.close()
        for handler in list(self.handlers):
            handler.cancel()
        await asyncio.gather(*list(self.handlers), return_exceptions=True)

    async def close_server(self) -> None:
        if self.server is not None:
            self.server.close()
        await self.disconnect_clients()
        if self.server is not None:
            await self.server.wait_closed()

    async def _states(self, writer: asyncio.StreamWriter) -> None:
        while True:
            if self.push_states:
                writer.write(
                    json.dumps(
                        {"jsonrpc": "2.0", "method": "robot.state", "params": self.state()}
                    ).encode()
                    + b"\n"
                )
                await writer.drain()
            await asyncio.sleep(0.1)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self.handlers.add(task)
        self.clients.add(writer)
        states: asyncio.Task[None] | None = None
        try:
            while line := await reader.readline():
                request = json.loads(line)
                method, params = request["method"], request.get("params")
                if "id" not in request:
                    await self.notify(method, params)
                    continue
                try:
                    result = await self.call(method, params)
                    response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
                except RobotRefused as error:
                    response = {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {"accepted": False, "reason": error.reason},
                    }
                except JsonRpcError as error:
                    response = {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "error": {"code": error.code, "message": error.message},
                    }
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
                if method == "robot.subscribe" and states is None:
                    states = asyncio.create_task(self._states(writer))
        except (OSError, LinkDown):
            pass
        finally:
            if states is not None:
                states.cancel()
                with contextlib.suppress(asyncio.CancelledError, OSError):
                    await states
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self.clients.discard(writer)
            if task is not None:
                self.handlers.discard(task)
