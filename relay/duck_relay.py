#!/usr/bin/env python3
"""Dependency-free, deny-by-default TCP bridge to the simulator's Unix sockets."""

import argparse
import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import math
import os
import signal
import sys
from pathlib import Path

# Direct execution still works with system Python, without installing Coin-Coin.
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coincoin.robot.methods import ROBOT_METHODS  # noqa: E402

FRAME_METHODS = frozenset({"hello", "media.frame"})
MAX_LINE = 64 * 1024
LOGGER = logging.getLogger("duck-relay")


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def _error(id_: object, code: int, message: str) -> bytes:
    return (
        json.dumps(
            {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}},
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
        + b"\n"
    )


def filter_line(line: bytes, methods: frozenset[str]) -> tuple[bytes | None, bytes | None]:
    """Return the original permitted line or a response, never echoing parameters."""
    if len(line.removesuffix(b"\n")) > MAX_LINE:
        LOGGER.warning("invalid request: line too long")
        return None, _error(None, -32600, "request too large")
    try:
        request = json.loads(line, parse_constant=_invalid_constant, parse_float=_finite_float)
    except (ValueError, UnicodeDecodeError, RecursionError):
        LOGGER.warning("invalid request: invalid JSON")
        return None, _error(None, -32600, "invalid request")
    if not isinstance(request, dict):
        LOGGER.warning("invalid request: not an object")
        return None, _error(None, -32600, "invalid request")
    id_ = request.get("id")
    valid_id = id_ is None or type(id_) in (int, float, str)
    method = request.get("method")
    if (
        not valid_id
        or request.get("jsonrpc") != "2.0"
        or not isinstance(method, str)
        or not method
        or ("params" in request and not isinstance(request["params"], (dict, list)))
    ):
        LOGGER.warning("invalid request: invalid envelope")
        return None, _error(id_ if valid_id else None, -32600, "invalid request")
    if method not in methods:
        LOGGER.warning("method refused: %r", method)
        response = _error(id_, -32601, "refusee par duck-relay") if "id" in request else None
        return None, response
    return line, None


async def read_line(reader: asyncio.StreamReader) -> tuple[bytes, bool]:
    """Drain an oversized line without keeping it, then resume at its delimiter."""
    oversized = False
    while True:
        try:
            line = await reader.readuntil(b"\n")
            return line, oversized or len(line) > MAX_LINE + 1
        except asyncio.LimitOverrunError as error:
            oversized = True
            await reader.readexactly(error.consumed)
        except asyncio.IncompleteReadError as error:
            return error.partial, oversized


class DuckRelay:
    """Own both listeners and all per-client Unix connections."""

    def __init__(
        self,
        state_dir: Path,
        *,
        duck: str = "duck-a",
        host: str = "127.0.0.1",
        robot_port: int = 7810,
        frame_port: int = 7811,
        token: str = "",
    ) -> None:
        if not ipaddress.ip_address(host).is_loopback and not token:
            raise ValueError("A non-loopback listener requires a token")
        if "\n" in token or "\r" in token or len(token.encode()) > MAX_LINE:
            raise ValueError("Invalid authentication token")
        if not duck or Path(duck).name != duck or duck in (".", ".."):
            raise ValueError("Duck must be a socket basename")
        self.state_dir = state_dir.expanduser()
        self.duck = duck
        self.host = host
        self.robot_port = robot_port
        self.frame_port = frame_port
        self._token = token.encode()
        self.servers: list[asyncio.Server] = []
        self._clients: set[asyncio.Task[None]] = set()
        self._closing = False

    async def start(self) -> None:
        self._closing = False
        try:
            for port, suffix, methods in (
                (self.robot_port, ".sock", ROBOT_METHODS),
                (self.frame_port, "-frame.sock", FRAME_METHODS),
            ):
                target = self.state_dir / f"{self.duck}{suffix}"

                def handle(
                    reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter,
                    target: Path = target,
                    methods: frozenset[str] = methods,
                ) -> None:
                    if self._closing:
                        writer.close()
                        return
                    task = asyncio.create_task(self._proxy(reader, writer, target, methods))
                    self._clients.add(task)

                    def completed(task: asyncio.Task[None]) -> None:
                        self._clients.discard(task)
                        writer.close()

                    task.add_done_callback(completed)

                self.servers.append(
                    await asyncio.start_server(handle, self.host, port, limit=MAX_LINE)
                )
                LOGGER.info("listening on %s:%s", self.host, port)
        except OSError:
            await self.close()
            raise

    async def close(self) -> None:
        self._closing = True
        for server in self.servers:
            server.close()
        clients = list(self._clients)
        for client in clients:
            client.cancel()
        await asyncio.gather(*clients, return_exceptions=True)
        for server in self.servers:
            await server.wait_closed()
        self.servers.clear()

    async def _proxy(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        target: Path,
        methods: frozenset[str],
    ) -> None:
        unix_writer: asyncio.StreamWriter | None = None
        pipes: list[asyncio.Task[None]] = []
        LOGGER.info("connection: %s", writer.get_extra_info("peername"))
        try:
            if self._token:
                auth, oversized = await asyncio.wait_for(read_line(reader), 3)
                if (
                    oversized
                    or not auth.endswith(b"\n")
                    or not hmac.compare_digest(auth[:-1], self._token)
                ):
                    LOGGER.warning("authentication refused")
                    return
            try:
                unix_reader, unix_writer = await asyncio.open_unix_connection(target)
            except OSError as error:
                LOGGER.error("Unix target unavailable: %s (%s)", target, type(error).__name__)
                return
            pipes = [
                asyncio.create_task(self._requests(reader, unix_writer, writer, methods)),
                asyncio.create_task(self._responses(unix_reader, writer)),
            ]
            done, _ = await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
            for pipe in done:
                pipe.result()
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as error:  # noqa: UP041
            LOGGER.warning("connection ended: %s", type(error).__name__)
        finally:
            for pipe in pipes:
                pipe.cancel()
            for output in (unix_writer, writer):
                if output is not None:
                    output.close()
            await asyncio.gather(*pipes, return_exceptions=True)
            for output in (unix_writer, writer):
                if output is not None:
                    with contextlib.suppress(OSError):
                        try:
                            await asyncio.wait_for(output.wait_closed(), 1)
                        except asyncio.TimeoutError:  # noqa: UP041 - Python 3.10 compatibility
                            LOGGER.warning("connection close timed out; aborting transport")
                            output.transport.abort()
            LOGGER.info("disconnection")

    async def _requests(
        self,
        reader: asyncio.StreamReader,
        unix_writer: asyncio.StreamWriter,
        writer: asyncio.StreamWriter,
        methods: frozenset[str],
    ) -> None:
        while True:
            line, oversized = await read_line(reader)
            if not line and not oversized:
                return
            if oversized or not line.endswith(b"\n"):
                LOGGER.warning("invalid request: oversized or incomplete line")
                forward, response = None, _error(None, -32600, "invalid request")
            else:
                forward, response = filter_line(line, methods)
            if response is not None:
                writer.write(response)
                await writer.drain()
            if forward is not None:
                unix_writer.write(forward)
                await unix_writer.drain()
            if not line.endswith(b"\n"):
                return

    async def _responses(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(64 * 1024):
            writer.write(data)
            await writer.drain()


async def run(args: argparse.Namespace) -> None:
    relay = DuckRelay(
        Path(args.state_dir),
        duck=args.duck,
        host=args.host,
        robot_port=args.robot_port,
        frame_port=args.frame_port,
        token=args.token,
    )
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    try:
        await relay.start()
        await stopped.wait()
    finally:
        await relay.close()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir", default=os.environ.get("DUCK_SIM_STATE", "~/.cache/duck-sim")
    )
    parser.add_argument("--duck", default="duck-a")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--robot-port", type=int, default=7810)
    parser.add_argument("--frame-port", type=int, default=7811)
    parser.add_argument("--token", default=os.environ.get("RELAY_TOKEN", ""))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(run(args))
    except (OSError, ValueError) as error:
        parser.exit(1, f"duck-relay: {error}\n")


if __name__ == "__main__":
    main()
