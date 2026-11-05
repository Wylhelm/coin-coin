"""Manual CLI for the governor and relay refusal diagnostics."""

import argparse
import asyncio
import json
from dataclasses import asdict
from typing import Any

from coincoin.clock import SystemClock
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.robot.governor import Governor, ToolResult
from coincoin.robot.jsonrpc import JsonRpcError
from coincoin.robot.link import LinkDown, RobotLink, RobotRefused
from coincoin.robot.tcp_link import TcpLink


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="coincoin-cli")
    result.add_argument("--link", help="Robot relay URL (otherwise ROBOT_LINK)")
    commands = result.add_subparsers(dest="command", required=True)
    for name, fields in (
        ("move", ("vx", "vy", "vyaw", "duration_s")),
        ("turn", ("degrees",)),
        ("pose", ("z", "roll", "pitch")),
    ):
        command = commands.add_parser(name)
        for field in fields:
            command.add_argument(field, type=float)
    commands.add_parser("look").add_argument("direction")
    commands.add_parser("do").add_argument("name")
    for name in ("sit", "stand", "skills", "status", "stop"):
        commands.add_parser(name)
    raw = commands.add_parser("raw", help="Diagnostic RPC through the filtering relay")
    raw.add_argument("method")
    raw.add_argument("params", nargs="?", default="{}")
    return result


async def execute(
    args: argparse.Namespace, settings: Settings, *, link: RobotLink | None = None
) -> tuple[dict[str, Any], int]:
    clock = SystemClock()
    if link is None:
        link = TcpLink(
            args.link or settings.robot_link,
            frame_url=settings.frame_link,
            token=settings.relay_token.get_secret_value(),
            clock=clock,
        )
    governor = Governor(link, settings, clock, Journal(retention_days=settings.log_retention_days))
    try:
        await link.connect()
        async with asyncio.timeout(5):
            while link.state() is None:
                await clock.sleep(0.05)
        before = link.state()
        match args.command:
            case "move":
                result = await governor.move(args.vx, args.vy, args.vyaw, args.duration_s)
            case "turn":
                result = await governor.turn(args.degrees)
            case "look":
                result = await governor.look(args.direction)
                if result.ok:
                    completed = await governor.wait_look()
                    if not completed.ok:
                        result = completed
            case "pose":
                result = await governor.set_pose(args.z, args.roll, args.pitch)
            case "sit":
                result = await governor.sit()
            case "stand":
                result = await governor.stand()
            case "do":
                result = await governor.do_skill(args.name)
            case "skills":
                return {"ok": True, "skills": await governor.skills()}, 0
            case "status":
                result = await governor.status()
            case "stop":
                result = await governor.stop("CLI stop")
            case "raw":
                params = json.loads(args.params)
                if not isinstance(params, dict):
                    raise ValueError("Raw parameters must be a JSON object")
                raw = await link.call(args.method, params)
                return {"ok": True, "result": raw}, 0
            case _:
                raise ValueError("Unknown CLI command")
        output = asdict(result)
        if result.ok and args.command in ("move", "turn"):
            completed = await governor.wait_motion()
            if completed is None:
                completed = ToolResult(False, reason="motion interrupted")
            await clock.sleep(0.15)
            if completed.ok:
                async with asyncio.timeout(1):
                    while True:
                        state = link.state()
                        if state is None:
                            raise LinkDown("Robot disconnected while settling")
                        applied = state.get("move", {}).get("applied")
                        if not isinstance(applied, list) or len(applied) != 3:
                            raise ValueError("Robot applied velocity unavailable")
                        if all(abs(value) < 0.01 for value in applied):
                            break
                        await clock.sleep(0.05)
            output.update(
                ok=completed.ok,
                detail=completed.detail,
                reason=completed.reason,
                state_before=before,
                state_after=link.state(),
            )
        return output, 0 if output["ok"] else 1
    except JsonRpcError as error:
        return {"ok": False, "code": error.code, "reason": error.message}, 1
    except TimeoutError:
        return {"ok": False, "reason": "robot operation timed out"}, 1
    except (LinkDown, RobotRefused, ValueError) as error:
        return {"ok": False, "reason": str(error)}, 1
    finally:
        await governor.close()
        await link.close()


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    output, code = asyncio.run(execute(args, Settings()))
    print(json.dumps(output, ensure_ascii=True, allow_nan=False))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
