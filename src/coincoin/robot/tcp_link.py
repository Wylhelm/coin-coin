"""Reconnectable robot TCP transport, including first-line relay authentication."""

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from coincoin.clock import Clock, SystemClock
from coincoin.robot.frames import FrameError, fetch_frame
from coincoin.robot.jsonrpc import API_VERSION, JsonRpcConnection, JsonRpcError
from coincoin.robot.link import Frame, LinkDown, RobotRefused

LOGGER = logging.getLogger(__name__)
LinkCallback = Callable[[], Awaitable[None] | None]


def tcp_address(url: str) -> tuple[str, int]:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "tcp"
        or parsed.hostname is None
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or not 0 < parsed.port < 65536
    ):
        raise ValueError("Expected tcp://host:port without credentials, path or query")
    return parsed.hostname, parsed.port


class TcpLink:
    def __init__(
        self,
        url: str,
        *,
        frame_url: str | None = None,
        token: str = "",
        clock: Clock | None = None,
        on_up: LinkCallback | None = None,
        on_down: LinkCallback | None = None,
    ) -> None:
        self._address = tcp_address(url)
        self._frame_address = tcp_address(frame_url) if frame_url else None
        if "\n" in token or "\r" in token:
            raise ValueError("Invalid authentication token")
        self._token = token
        self._clock = clock if clock is not None else SystemClock()
        self.on_up = on_up
        self.on_down = on_down
        self._connection: JsonRpcConnection | None = None
        self._runner: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._up = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connection is not None and self._connection.connected

    async def connect(self) -> None:
        if self.connected:
            return
        if self._runner is None:
            self._ready = asyncio.get_running_loop().create_future()
            self._runner = asyncio.create_task(self._run())
            await asyncio.shield(self._ready)
        else:
            try:
                async with asyncio.timeout(2):
                    while not self.connected:
                        self._up.clear()
                        await self._up.wait()
            except TimeoutError as error:
                raise LinkDown("Robot TCP link has not reconnected") from error

    async def close(self) -> None:
        runner, self._runner = self._runner, None
        if runner is not None:
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner
        if self._ready is not None and not self._ready.done():
            self._ready.cancel()
        self._ready = None
        self._up.clear()

    def state(self) -> dict[str, Any] | None:
        return self._connection.state() if self._connection is not None else None

    def state_age(self) -> float:
        return self._connection.state_age() if self._connection is not None else float("inf")

    def _require_connection(self) -> JsonRpcConnection:
        if self._connection is None or not self.connected:
            raise LinkDown("Robot TCP link is down")
        return self._connection

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 2.0,  # noqa: ASYNC109 - RobotLink contract
    ) -> Any:
        return await self._require_connection().call(method, params, timeout)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._require_connection().notify(method, params)

    async def frame(self) -> Frame:
        if self._frame_address is None:
            raise FrameError("FRAME_LINK is not configured")
        return await fetch_frame(*self._frame_address, token=self._token)

    async def _callback(self, callback: LinkCallback | None) -> None:
        if callback is not None:
            result = callback()
            if result is not None:
                await result

    async def _open(self) -> JsonRpcConnection:
        writer: asyncio.StreamWriter | None = None
        connection: JsonRpcConnection | None = None
        initialized = False
        try:
            async with asyncio.timeout(2):
                reader, writer = await asyncio.open_connection(*self._address)
                if self._token:
                    writer.write(self._token.encode() + b"\n")
                    await writer.drain()
                connection = JsonRpcConnection(reader, writer, clock=self._clock)
                try:
                    await connection.call("hello", {"api_version": API_VERSION})
                except JsonRpcError:
                    LOGGER.info("hello rejected; trying the state subscription")
                await connection.call("robot.subscribe", {"hz": 10})
                await connection.notify("robot.mouth", {"open": 0})
                initialized = True
                return connection
        finally:
            if connection is not None and not initialized:
                await connection.close()
            elif writer is not None and connection is None:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()

    async def _run(self) -> None:
        delay = 0.5
        connection: JsonRpcConnection | None = None
        try:
            while True:
                try:
                    connection = await self._open()
                except (OSError, TimeoutError, LinkDown, JsonRpcError, RobotRefused) as error:
                    LOGGER.warning("Robot connection failed (%s)", type(error).__name__)
                    if self._ready is not None and not self._ready.done():
                        self._ready.set_exception(LinkDown("Could not initialize robot TCP link"))
                        self._ready.exception()
                else:
                    self._connection = connection
                    delay = 0.5
                    await self._callback(self.on_up)
                    self._up.set()
                    if self._ready is not None and not self._ready.done():
                        self._ready.set_result(None)
                    await connection.wait_closed()
                    self._up.clear()
                    self._connection = None
                    await connection.close()
                    connection = None
                    await self._callback(self.on_down)
                await self._clock.sleep(min(30, delay * random.uniform(0.8, 1.2)))
                delay = min(30, delay * 2)
        finally:
            was_up = self._connection is not None
            self._connection = None
            self._up.clear()
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(LinkDown("Robot TCP initialization stopped"))
                self._ready.exception()
            if connection is not None:
                await connection.close()
            if was_up:
                await self._callback(self.on_down)
