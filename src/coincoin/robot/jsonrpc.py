"""JSON-RPC correlation and state streaming, independent of TCP or WebRTC."""

import asyncio
import contextlib
import itertools
import json
import logging
import math
from typing import Any, Protocol

from coincoin.clock import Clock, SystemClock
from coincoin.robot.link import LinkDown, RobotRefused

API_VERSION = 47
LOGGER = logging.getLogger(__name__)


class RpcReader(Protocol):
    async def readline(self) -> bytes: ...


class RpcWriter(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"{code}: {message}")


def encode_request(id_: int | None, method: str, params: dict[str, Any] | None) -> bytes:
    request: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        request["id"] = id_
    if params is not None:
        request["params"] = params
    return json.dumps(request, allow_nan=False, separators=(",", ":")).encode() + b"\n"


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def decode_message(line: bytes) -> dict[str, Any]:
    try:
        message = json.loads(line, parse_constant=_invalid_constant, parse_float=_finite_float)
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise LinkDown("Invalid JSON-RPC message") from error
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise LinkDown("Invalid JSON-RPC envelope")
    return message


def response_result(message: dict[str, Any]) -> Any:
    if "error" in message:
        error = message["error"]
        if (
            not isinstance(error, dict)
            or type(error.get("code")) is not int
            or not isinstance(error.get("message"), str)
        ):
            raise LinkDown("Invalid JSON-RPC error")
        raise JsonRpcError(error["code"], error["message"], error.get("data"))
    if "result" not in message:
        raise LinkDown("Missing JSON-RPC result")
    result = message["result"]
    if isinstance(result, dict) and result.get("accepted") is False:
        reason = result.get("reason")
        raise RobotRefused(reason if isinstance(reason, str) else "robot refused")
    return result


class JsonRpcConnection:
    def __init__(self, reader: RpcReader, writer: RpcWriter, *, clock: Clock | None = None) -> None:
        self._reader = reader
        self._writer = writer
        self._clock = clock if clock is not None else SystemClock()
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._state: dict[str, Any] | None = None
        self._state_at = -math.inf
        self._closed = asyncio.Event()
        self._connected = True
        self._reader_task = asyncio.create_task(self._read_loop())

    @property
    def connected(self) -> bool:
        return self._connected

    def state(self) -> dict[str, Any] | None:
        return self._state

    def state_age(self) -> float:
        return self._clock.now() - self._state_at if self._connected else math.inf

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def close(self) -> None:
        self._finish("Robot connection closed")
        self._reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reader_task
        with contextlib.suppress(OSError):
            await self._writer.wait_closed()

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 2.0,  # noqa: ASYNC109 - RobotLink contract
    ) -> Any:
        if not self._connected:
            raise LinkDown("Robot connection is down")
        id_ = next(self._ids)
        wire = encode_request(id_, method, params)
        future = asyncio.get_running_loop().create_future()
        self._pending[id_] = future
        try:
            async with asyncio.timeout(timeout):
                await self._send(wire)
                return await future
        finally:
            self._pending.pop(id_, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        wire = encode_request(None, method, params)
        try:
            async with asyncio.timeout(0.05):
                await self._send(wire)
        except TimeoutError as error:
            self._finish("Robot notification exceeded 50 ms")
            raise LinkDown("Robot notification exceeded 50 ms") from error

    async def _send(self, wire: bytes) -> None:
        async with self._write_lock:
            if not self._connected:
                raise LinkDown("Robot connection is down")
            drains: list[asyncio.Task[Any]] = []
            try:
                self._writer.write(wire)
                drain = asyncio.create_task(self._writer.drain())
                closed = asyncio.create_task(self._closed.wait())
                drains = [drain, closed]
                await asyncio.wait(drains, return_when=asyncio.FIRST_COMPLETED)
                if not self._connected:
                    raise LinkDown("Robot connection is down")
                await drain
            except OSError as error:
                self._finish("Robot write failed")
                raise LinkDown("Robot write failed") from error
            finally:
                for task in drains:
                    task.cancel()
                await asyncio.gather(*drains, return_exceptions=True)

    def _finish(self, reason: str) -> None:
        if not self._connected:
            return
        self._connected = False
        self._state = None
        self._state_at = -math.inf
        for future in self._pending.values():
            if not future.done():
                future.set_exception(LinkDown(reason))
        self._writer.close()
        self._closed.set()

    async def _read_loop(self) -> None:
        try:
            while line := await self._reader.readline():
                message = decode_message(line)
                if message.get("method") == "robot.state" and "id" not in message:
                    state = message.get("params")
                    if not isinstance(state, dict):
                        raise LinkDown("Invalid robot.state")
                    self._state = state
                    self._state_at = self._clock.now()
                    continue
                id_ = message.get("id")
                future = self._pending.get(id_) if type(id_) is int else None
                if future is None or future.done():
                    continue
                try:
                    future.set_result(response_result(message))
                except (JsonRpcError, RobotRefused) as error:
                    future.set_exception(error)
        except (OSError, ValueError, LinkDown) as error:
            LOGGER.warning("Robot reader failed (%s)", type(error).__name__)
        finally:
            self._finish("Robot reader disconnected")
