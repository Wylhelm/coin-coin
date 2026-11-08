"""Manual CLI for the governor and relay refusal diagnostics."""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from typing import Any

from coincoin.clock import SystemClock
from coincoin.config import Settings
from coincoin.doctor import run_doctor
from coincoin.journal import Journal
from coincoin.robot.factory import create_robot_link
from coincoin.robot.governor import Governor, ToolResult
from coincoin.robot.jsonrpc import JsonRpcError
from coincoin.robot.link import LinkDown, RobotLink, RobotRefused


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="coincoin-cli")
    result.add_argument("--link", help="Robot URL (otherwise ROBOT_LINK)")
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
    commands.add_parser("mode").add_argument("mode", choices=("walk", "roller"))
    for name in ("sit", "stand", "skills", "status", "stop"):
        commands.add_parser(name)
    raw = commands.add_parser("raw", help="Diagnostic RPC with the S1 method filter")
    raw.add_argument("method")
    raw.add_argument("params", nargs="?", default="{}")
    doctor = commands.add_parser(
        "doctor", help="Check every step of the robot connection and print fixes"
    )
    doctor.add_argument("--json", action="store_true", help="Machine-readable report")
    return result


async def execute(
    args: argparse.Namespace, settings: Settings, *, link: RobotLink | None = None
) -> tuple[dict[str, Any], int]:
    clock = SystemClock()
    if link is None:
        link_url = args.link or settings.robot_link
        link = create_robot_link(
            link_url,
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
            case "mode":
                result = await governor.set_mode(args.mode)
                if result.ok and governor.awaiting_person:
                    # The link refused robot.setMode: wait like the voice service does.
                    print(result.detail, file=sys.stderr, flush=True)
                    await governor.wait_person()
                    current = await link.call("robot.mode")
                    reached = isinstance(current, dict) and current.get("mode") == args.mode
                    result = (
                        ToolResult(True, detail=f"locomotion mode: {args.mode} (set by a person)")
                        if reached
                        else ToolResult(False, reason=f"{args.mode} mode was not selected in time")
                    )
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
    if args.command == "doctor":
        report = asyncio.run(run_doctor(Settings(), args.link))
        print(
            json.dumps(report.as_dict(), ensure_ascii=True, allow_nan=False)
            if args.json
            else report.text()
        )
        raise SystemExit(0 if report.ok else 1)
    output, code = asyncio.run(execute(args, Settings()))
    print(json.dumps(output, ensure_ascii=True, allow_nan=False))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
