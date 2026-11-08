#!/usr/bin/env python3
"""Record a robot session to JSONL for offline replay (see scripts/session_log.py).

Passive by design: it connects, subscribes like coin-coin does, and records ``robot.state``,
periodic ``robot.health`` (battery, thermal, loop rate), camera snapshots and, optionally, the
ToF stream. It sends no motion command. Drive the robot with the pad or ``robotctl`` while it
records, and type a note + Enter (``--notes``) to mark what you did ("lifted", "walk 1 m").

    uv run python -m scripts.record_session --link webrtc://duck-xxxx.local:8443 \
        --duration 120 --out recordings/first-walk.jsonl --notes

On a WebRTC link the camera comes from the video track; on TCP it is ``media.frame`` through the
relay (``--frame-link``). ToF is not on coin-coin's link at all (``tof.stream`` is a separate
``tofd`` socket on the robot); ``--tof tcp://host:port`` reads it from any line-delimited
JSON-RPC TCP endpoint that forwards that socket, e.g. a ``socat`` you start on the robot
(unverified on hardware; see docs/robot-bringup.md).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coincoin.clock import Clock, SystemClock
from coincoin.robot.jsonrpc import API_VERSION
from coincoin.robot.link import RobotLink
from scripts.session_log import RecordingLink, SessionWriter, StateSampler


def positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return number


def nonnegative(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--link", default=os.environ.get("ROBOT_LINK", "tcp://127.0.0.1:7810"))
    parser.add_argument("--frame-link", default=os.environ.get("FRAME_LINK"))
    parser.add_argument("--token", default=os.environ.get("RELAY_TOKEN", ""))
    parser.add_argument("--duration", type=positive, default=120.0, help="seconds (default 120)")
    parser.add_argument("--out", type=Path, required=True, help="JSONL file to write")
    parser.add_argument("--state-hz", type=positive, default=20.0, help="state sampling rate")
    parser.add_argument(
        "--frame-every", type=nonnegative, default=1.0, help="seconds between snapshots; 0 = none"
    )
    parser.add_argument(
        "--health-every", type=nonnegative, default=5.0, help="seconds between robot.health"
    )
    parser.add_argument("--tof", help="tcp://host:port serving tof.stream (optional)")
    parser.add_argument("--tof-hz", type=int, default=10)
    parser.add_argument("--notes", action="store_true", help="read operator notes from stdin")
    parser.add_argument("--label", default="", help="free text stored in the header")
    args = parser.parse_args(argv)
    if urlsplit(args.link).scheme not in ("tcp", "webrtc"):
        parser.error("--link must be tcp://host:port or webrtc://host:port")
    if args.tof and urlsplit(args.tof).scheme != "tcp":
        parser.error("--tof must be tcp://host:port")
    return args


async def _probe(link: RobotLink, method: str) -> tuple[str, Any]:
    try:
        return "ok", await link.call(method, {}, 2.0)
    except Exception as error:
        return "error", f"{type(error).__name__}: {error}"


async def write_header(link: RobotLink, writer: SessionWriter, args: argparse.Namespace) -> None:
    probes = {}
    for method in ("robot.policies", "robot.mode", "robot.health", "robot.skills"):
        status, value = await _probe(link, method)
        probes[method] = value if status == "ok" else {"error": value}
    writer.header(
        link=urlsplit(args.link).scheme,
        host=urlsplit(args.link).hostname,
        api_version=API_VERSION,
        label=args.label,
        duration_requested=args.duration,
        probes=probes,
    )


async def record_tof(
    url: str, hz: int, writer: SessionWriter, stop: asyncio.Event
) -> None:  # pragma: no cover - needs a tofd endpoint
    parts = urlsplit(url)
    try:
        reader, stream = await asyncio.open_connection(parts.hostname, parts.port)
    except OSError as error:
        writer.write("note", text=f"tof unavailable: {error}")
        return
    try:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tof.stream", "params": {"hz": hz}}
        stream.write((json.dumps(request) + "\n").encode())
        await stream.drain()
        while not stop.is_set():
            line = await reader.readline()
            if not line:
                writer.write("note", text="tof stream closed")
                return
            message = json.loads(line)
            if message.get("method") == "tof.frame":
                writer.write("tof", frame=message.get("params"))
            elif "id" in message:
                writer.write("note", text=f"tof.stream answered {message}")
    finally:
        stream.close()


def start_notes(writer: SessionWriter, loop: asyncio.AbstractEventLoop) -> None:
    def read() -> None:  # pragma: no cover - interactive
        for line in sys.stdin:
            text = line.strip()
            if text:
                loop.call_soon_threadsafe(lambda text=text: writer.write("note", text=text))

    threading.Thread(target=read, daemon=True).start()


async def record(
    link: RobotLink,
    writer: SessionWriter,
    args: argparse.Namespace,
    clock: Clock | None = None,
) -> dict[str, int]:
    """Record from an already-constructed link until ``args.duration`` elapses."""
    clock = clock if clock is not None else SystemClock()
    recording = RecordingLink(link, writer)
    await recording.connect()
    stop = asyncio.Event()
    tof_task = None
    try:
        await write_header(link, writer, args)
        if args.tof:
            tof_task = asyncio.create_task(record_tof(args.tof, args.tof_hz, writer, stop))
        sampler = StateSampler(recording, writer)
        period = 1.0 / args.state_hz
        start = clock.now()
        next_frame = start
        next_health = start + args.health_every
        while clock.now() - start < args.duration:
            sampler.sample()
            now = clock.now()
            if args.frame_every and now >= next_frame:
                next_frame = now + args.frame_every
                try:
                    await recording.frame()
                except Exception as error:
                    writer.write("note", text=f"frame unavailable: {type(error).__name__}: {error}")
            if args.health_every and now >= next_health:
                next_health = now + args.health_every
                with contextlib.suppress(Exception):
                    await recording.call("robot.health", {}, 2.0)
            await clock.sleep(period)
    finally:
        stop.set()
        if tof_task is not None:
            tof_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tof_task
        await recording.close()
    return dict(writer.counts)


async def main(argv: list[str] | None = None) -> int:  # pragma: no cover - needs a robot
    from coincoin.robot.factory import create_robot_link

    args = parse_args(argv)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    link = create_robot_link(args.link, frame_url=args.frame_link, token=args.token)
    with args.out.open("w", encoding="utf-8") as handle:
        writer = SessionWriter(handle)
        if args.notes:
            start_notes(writer, asyncio.get_running_loop())
        counts = await record(link, writer, args)
    print(json.dumps({"out": str(args.out), "counts": counts}))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(main()))
