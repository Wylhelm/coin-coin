#!/usr/bin/env python3
"""Simulator stand-in for the person in the room who selects walk/roller on the duck.

Over WebRTC, mediad refuses ``robot.setMode`` (route.rs: mode changes belong to someone next to
the robot). On the delivered robot a person fits or removes the skates and selects the mode
locally; in the simulator this script does the local part by calling robotd's own Unix socket,
which is exactly what a local client on the duck would do. Never point it at real hardware.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("walk", "roller"))
    parser.add_argument(
        "--socket",
        type=Path,
        help="robotd socket; default $DUCK_SIM_STATE/duck-a.sock",
    )
    parser.add_argument("--delay", type=float, default=0.0, help="seconds to wait first")
    return parser.parse_args(argv)


async def call(socket: Path, method: str, params: dict[str, object] | None = None) -> object:
    reader, writer = await asyncio.open_unix_connection(socket)
    try:
        for index, (name, arguments) in enumerate(
            (("hello", {"api_version": 47}), (method, params)), start=1
        ):
            request: dict[str, object] = {"jsonrpc": "2.0", "id": index, "method": name}
            if arguments is not None:
                request["params"] = arguments
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            while True:
                answer = json.loads(await asyncio.wait_for(reader.readline(), 30))
                if answer.get("id") == index:
                    break
            if "error" in answer:
                raise RuntimeError(f"{name}: {answer['error']}")
        return answer.get("result")
    finally:
        writer.close()
        await writer.wait_closed()


async def main_async(args: argparse.Namespace) -> int:
    socket = args.socket
    if socket is None:
        state = os.environ.get("DUCK_SIM_STATE")
        if not state:
            print("Set DUCK_SIM_STATE or pass --socket", file=sys.stderr)
            return 2
        socket = Path(state) / "duck-a.sock"
    if args.delay > 0:
        await asyncio.sleep(args.delay)
    result = await call(socket, "robot.setMode", {"mode": args.mode})
    print(json.dumps({"mode": args.mode, "result": result}))
    accepted = isinstance(result, dict) and result.get("accepted") is not False
    return 0 if accepted else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
