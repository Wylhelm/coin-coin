"""Local equipment assistant, explicitly restricted to a simulated robotd."""

import asyncio
import json
from pathlib import Path

from coincoin.robot.jsonrpc import API_VERSION
from coincoin.robot.link import RobotRefused


async def select_mode(socket: Path, mode: str) -> None:
    if mode not in ("walk", "roller"):
        raise RobotRefused("unsupported simulation equipment mode")
    reader, writer = await asyncio.open_unix_connection(socket)
    try:
        for index, (method, params) in enumerate(
            (
                ("hello", {"api_version": API_VERSION}),
                ("robot.policies", None),
                ("robot.setMode", {"mode": mode}),
            ),
            start=1,
        ):
            request = {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    raise RobotRefused("simulation equipment socket disconnected")
                answer = json.loads(line)
                if answer.get("id") == index:
                    break
            if "error" in answer:
                raise RobotRefused(f"simulation equipment {method}: {answer['error']}")
            result = answer.get("result")
            if method == "robot.policies" and (
                not isinstance(result, dict) or result.get("simulated") is not True
            ):
                raise RobotRefused("equipment assistance requires an explicit simulated robot")
            if method == "robot.setMode" and (
                not isinstance(result, dict) or result.get("accepted") is not True
            ):
                raise RobotRefused(f"simulation equipment mode change refused: {result}")
    finally:
        writer.close()
        await writer.wait_closed()
