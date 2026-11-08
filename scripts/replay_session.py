#!/usr/bin/env python3
"""Inspect, compare and serve JSONL session recordings offline (see scripts/session_log.py).

    uv run python -m scripts.replay_session summary recordings/real.jsonl
    uv run python -m scripts.replay_session compare recordings/sim.jsonl recordings/real.jsonl
    uv run python -m scripts.replay_session serve recordings/real.jsonl --port 7810

``serve`` speaks the same line-delimited JSON-RPC as robotd behind coin-coin's TCP relay, so a
whole coin-coin can run against a recording with ``ROBOT_LINK=tcp://127.0.0.1:7810``. It pushes the
recorded ``robot.state`` after ``robot.subscribe`` and answers calls from the recording. Camera
frames are not served over TCP (``media.frame`` is raw UYVY); tests that need them use
``ReplayLink.frame()`` in-process.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coincoin.clock import Clock, SystemClock
from coincoin.robot.link import LinkDown, RobotRefused
from scripts.session_log import Event, ReplayLink, read_events, summarize

# Values whose difference between a sim and a real recording is worth a line in `compare`.
COMPARED = ("api_version", "duration_s", "state_hz", "state_gap_max_s", "loop_hz_min")


def compare(a: list[Event], b: list[Event]) -> dict[str, Any]:
    """Side-by-side summaries plus the differences that usually matter for sim/real parity."""
    left, right = summarize(a), summarize(b)
    differences: dict[str, Any] = {}
    for key in COMPARED:
        if left.get(key) != right.get(key):
            differences[key] = [left.get(key), right.get(key)]
    for key in ("safety_true_samples", "commands", "counts"):
        names = set(left[key]) | set(right[key])
        for name in sorted(names):
            pair = [left[key].get(name, 0), right[key].get(name, 0)]
            if pair[0] != pair[1]:
                differences[f"{key}.{name}"] = pair
    probes = (a[0].data.get("probes") or {}, b[0].data.get("probes") or {})
    for method in sorted(set(probes[0]) | set(probes[1])):
        if probes[0].get(method) != probes[1].get(method):
            differences[f"probe.{method}"] = [probes[0].get(method), probes[1].get(method)]
    return {"a": left, "b": right, "differences": differences}


class ReplayServer:
    """A TCP robotd stand-in fed by a ReplayLink."""

    def __init__(
        self,
        events: list[Event],
        *,
        token: str = "",
        speed: float = 1.0,
        loop: bool = False,
        clock: Clock | None = None,
    ) -> None:
        self.events = events
        self.token = token
        self.speed = speed
        self.loop = loop
        self.clock = clock if clock is not None else SystemClock()
        self.links: list[ReplayLink] = []
        self.server: asyncio.Server | None = None

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self.server = await asyncio.start_server(self._handle, host, port)
        return self.server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _push(self, link: ReplayLink, writer: asyncio.StreamWriter, hz: float) -> None:
        last: Any = None
        while True:
            state = link.state()
            if state is not None and state != last and link.state_age() < 1.0:
                last = state
                message = {"jsonrpc": "2.0", "method": "robot.state", "params": state}
                writer.write(json.dumps(message).encode() + b"\n")
                await writer.drain()
            await self.clock.sleep(1.0 / hz)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        link = ReplayLink(self.events, clock=self.clock, speed=self.speed, loop=self.loop)
        self.links.append(link)
        pusher: asyncio.Task[None] | None = None
        try:
            if self.token:
                line = await reader.readline()
                if line.decode(errors="replace").rstrip("\r\n") != self.token:
                    return
            await link.connect()
            while line := await reader.readline():
                request = json.loads(line)
                method, params = request.get("method"), request.get("params")
                if "id" not in request:
                    await link.notify(str(method), params)
                    continue
                try:
                    result = await link.call(str(method), params)
                    response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
                except RobotRefused as error:
                    response = {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {"accepted": False, "reason": error.reason},
                    }
                except LinkDown as error:
                    response = {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "error": {"code": -32000, "message": str(error)},
                    }
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
                if method == "robot.subscribe" and pusher is None:
                    hz = (params or {}).get("hz") or 50
                    pusher = asyncio.create_task(self._push(link, writer, float(hz)))
        except (OSError, ValueError):
            pass
        finally:
            if pusher is not None:
                pusher.cancel()
                with contextlib.suppress(asyncio.CancelledError, OSError):
                    await pusher
            await link.close()
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    summary = commands.add_parser("summary", help="counts, rates, safety flags, commands")
    summary.add_argument("recording", type=Path)
    pair = commands.add_parser("compare", help="differences between two recordings")
    pair.add_argument("a", type=Path)
    pair.add_argument("b", type=Path)
    serve = commands.add_parser("serve", help="serve a recording as a TCP robotd")
    serve.add_argument("recording", type=Path)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7810)
    serve.add_argument("--token", default="")
    serve.add_argument("--speed", type=float, default=1.0)
    serve.add_argument("--loop", action="store_true")
    return parser.parse_args(argv)


async def _serve(args: argparse.Namespace) -> int:  # pragma: no cover - runs until interrupted
    server = ReplayServer(
        read_events(args.recording), token=args.token, speed=args.speed, loop=args.loop
    )
    port = await server.start(args.host, args.port)
    print(f"replaying {args.recording} on tcp://{args.host}:{port}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await server.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "summary":
        print(json.dumps(summarize(read_events(args.recording)), indent=2))
        return 0
    if args.command == "compare":
        result = compare(read_events(args.a), read_events(args.b))
        print(json.dumps(result, indent=2))
        return 0
    return asyncio.run(_serve(args))  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
