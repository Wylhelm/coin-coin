"""Replay a navigation session on the live launcher's stack: duck-sim, mediad and WebRTC.

    .venv/bin/python -m tests.integration.nav_repro_live \\
        --realism real --seed 0 --out logs/nav_repro/live0.jsonl \\
        mode:roller mode:walk "ma chambre" salon status

Add `--record` to also save every ToF frame with the odometry and head pose it was projected
with (`<out>.frames.json`), for offline analysis.

The same path as `coincoin-appartement.sh --link webrtc --sim-auto-equipment --headless`, minus the
voice: the depth stream rides the WebRTC link and a mode change goes through the local simulation
equipment assistant. Steps are place names (walking go_to), `mode:walk|roller`, `turn:DEG`,
`move:VX:SECONDS` and `roller:PLACE` (a skating go_to, expected to be refused). The true pose
comes from the simulator's `oracle.pose` for scoring only; the controller never reads it.
`--expect-free-route` exits nonzero for unexpected refusals or walking arrivals farther than
0.20 m from the target, confident wrong positions, or physics-step contacts with world boxes.
Contact monitoring is injected only into this isolated simulator; the controller never reads it.
Refuses to start when the live launcher's ports or lock are in use.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from coincoin.clock import SystemClock
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.robot.capabilities import probe
from coincoin.robot.governor import Governor, ImmediateToolResult
from coincoin.robot.localization import EstimatedPose, LinkTofStream
from coincoin.robot.navigation import NavigationMap, Navigator, SimPose
from coincoin.robot.webrtc_link import WebRtcLink
from tests.integration.nav_repro import Trace, summarize
from tests.integration.sim_stack import COIN, CONTACT_BOOTSTRAP, STREAM, apartment_dir

sys.path.insert(0, str(COIN / "scripts"))
import live_stack  # noqa: E402  # pyright: ignore[reportMissingImports]


def read_contact_events(path: Path) -> int:
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if not rows or rows[0] != {"ready": True}:
        raise RuntimeError("the isolated simulator did not enable contact monitoring")
    for expected, row in enumerate(rows[1:], 1):
        if row.get("event") != expected:
            raise RuntimeError("the contact monitor produced an incomplete event log")
    return len(rows) - 1


class LiveStack:
    """Diagnostic truth and contact events, separate from the controller's inputs."""

    def __init__(self, link: WebRtcLink, truth: SimPose, contact_log: Path) -> None:
        self.link = link
        self.truth = truth
        self.contact_log = contact_log

    def contact_events(self) -> int:
        return read_contact_events(self.contact_log)

    async def probe(self) -> dict[str, Any]:
        pose = await self.truth.read()
        return {
            "position": [pose.x, pose.y],
            "yaw": pose.yaw,
            "contact_count": None,
            "contact_events": self.contact_events(),
        }


async def run_steps(args: argparse.Namespace, state: Path, out: Any) -> list[dict[str, Any]]:
    nav_map = NavigationMap.load(apartment_dir() / "navigation_appartement.json")
    link = WebRtcLink("webrtc://127.0.0.1:8443", clock=SystemClock())
    await link.connect()
    async with asyncio.timeout(30):
        while link.state() is None:  # noqa: ASYNC110 - bounded network observation
            await asyncio.sleep(0.1)
    localizer = EstimatedPose(nav_map, link.state, LinkTofStream(link), clock=SystemClock().now)
    recorded: list[dict[str, Any]] = []
    if args.record:
        observe = localizer.observe_frame

        def recording(frame: Any) -> Any:
            def particles() -> list[float] | None:
                if not localizer.filter.initialized:
                    return None
                pose, std_xy, std_yaw = localizer.filter.estimate()
                return [pose.x, pose.y, pose.yaw, std_xy, std_yaw]

            found = localizer._odom_at(frame.t_ns)  # noqa: SLF001 - diagnosis
            row = None
            if found is not None:
                odom, state = found
                previous = localizer._last_odom  # noqa: SLF001 - diagnosis
                row = {
                    "t_ns": frame.t_ns,
                    "distance_m": frame.distance_m.tolist(),
                    "status": frame.status.tolist(),
                    "odom": [odom.x, odom.y, odom.yaw, odom.z],
                    "previous_odom": (
                        [previous.x, previous.y, previous.yaw, previous.z]
                        if previous is not None
                        else None
                    ),
                    "imu": state["imu"]["quat"],
                    "tof": state["frames"]["tof"],
                    "state_t_ns": state.get("t_ns"),
                    "phase": trace_phase[0],
                    "particles_before": particles(),
                }
            rejected_before = localizer.stats.rejected_corrections
            scan = observe(frame)
            if row is not None:
                row["particles_after"] = particles()
                row["correction_rejected"] = localizer.stats.rejected_corrections > rejected_before
                recorded.append(row)
            return scan

        localizer.observe_frame = recording  # type: ignore[method-assign]
    trace_phase = ["start"]
    print(localizer.start_at(args.start), flush=True)
    localizer.start()
    truth = SimPose("tcp://127.0.0.1:7801")
    navigator = Navigator(nav_map, localizer, oracle=truth, localizer=localizer)
    governor = Governor(
        link,
        Settings(
            max_move_s=30,
            walk_assist=True,
            sim_equipment_socket=str(state / "duck-a.sock") if args.auto_equipment else "",
        ),
        SystemClock(),
        Journal(Path(args.out).with_suffix("") / "journal"),
        navigator=navigator,
    )
    governor.capabilities = await probe(link)
    await governor.start()
    stack = LiveStack(link, truth, state / "contacts.jsonl")
    trace = Trace(stack, localizer, out)  # type: ignore[arg-type]
    tracer = asyncio.create_task(trace.run())
    results: list[dict[str, Any]] = []

    async def stop_after_contact() -> None:
        while True:
            if stack.contact_events():
                await governor.stop(reason="diagnostic collision")
                return
            await asyncio.sleep(0.1)

    contact_watch = asyncio.create_task(stop_after_contact())
    try:
        trace.phase = "stand"
        prepared = await governor.stand()
        print(json.dumps({"stand": prepared.ok, "detail": prepared.detail or prepared.reason}))
        if prepared.ok:
            await asyncio.sleep(4.0)
        else:
            results.append(
                {
                    "step": "stand",
                    "ok": False,
                    "reason": prepared.reason,
                    "contact_events": stack.contact_events(),
                }
            )
        for step in args.steps:
            if not prepared.ok or stack.contact_events():
                break
            contacts_before = stack.contact_events()
            rejected_before = localizer.stats.rejected_corrections
            conflicts_before = localizer.stats.tracking_conflicts
            fit_conflicts_before = localizer.stats.fit_conflicts
            samples_before = len(trace.rows.get(step, []))
            settle_phase = f"{step}:settle"
            settle_before = len(trace.rows.get(settle_phase, []))
            trace.phase = step
            trace_phase[0] = step
            began = time.monotonic()
            kind, _, rest = step.partition(":")
            if kind == "mode" and rest:
                started = await governor.set_mode(rest)
                await governor.wait_person()
                result = started
                if started.ok and governor._mode != rest:  # noqa: SLF001 - diagnosis
                    result = type(started)(False, reason=f"mode is {governor._mode}")  # noqa: SLF001
            elif kind == "turn" and rest:
                started = await governor.turn(float(rest))
                result = started if not started.ok else await governor.wait_motion()
            elif kind == "move" and rest:
                vx, seconds = (float(value) for value in rest.split(":"))
                started = await governor.move(vx, 0, 0, seconds)
                result = started if not started.ok else await governor.wait_motion()
            elif kind == "roller" and rest:
                started = await governor.go_to(rest, mode="roller")
                result = started if not started.ok else await governor.wait_motion()
            elif kind == "status":
                result = await governor.status()
            else:
                started = await governor.go_to(step, mode="walk")
                result = (
                    started
                    if not started.ok or isinstance(started, ImmediateToolResult)
                    else await governor.wait_motion()
                )
            trace.phase = settle_phase
            await asyncio.sleep(2.0)
            row = await trace.sample()
            contact_events = stack.contact_events() - contacts_before
            samples = [
                *trace.rows.get(step, [])[samples_before:],
                *trace.rows.get(settle_phase, [])[settle_before:],
                row,
            ]
            summary = {
                "step": step,
                "ok": bool(result and result.ok),
                "detail": result.detail if result else None,
                "reason": result.reason if result else None,
                "seconds": round(time.monotonic() - began, 1),
                **summarize(samples),
                "contact_events": contact_events,
                "rejected_corrections": localizer.stats.rejected_corrections - rejected_before,
                "tracking_conflicts": localizer.stats.tracking_conflicts - conflicts_before,
                "fit_conflicts": localizer.stats.fit_conflicts - fit_conflicts_before,
                "after": row,
            }
            if step in navigator.places:
                final = await stack.probe()
                summary["final_m"] = math.dist(final["position"], navigator.places[step])
            results.append(summary)
            print(json.dumps(summary, ensure_ascii=False), flush=True)
            out.write(json.dumps({"summary": summary}, ensure_ascii=False) + "\n")
            if stack.contact_events():
                break
    finally:
        tracer.cancel()
        contact_watch.cancel()
        await governor.close()
        await navigator.close()
        await truth.close()
        await link.close()
        with contextlib.suppress(asyncio.CancelledError):
            await tracer
        with contextlib.suppress(asyncio.CancelledError):
            await contact_watch
        if args.record:
            Path(args.out).with_suffix(".frames.json").write_text(json.dumps(recorded))
    if stack.contact_events() and not any(row["contact_events"] for row in results):
        results.append(
            {
                "step": "startup",
                "ok": False,
                "reason": "contact before a recorded step",
                "contact_events": stack.contact_events(),
            }
        )
    return results


def free_route_failures(results: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    for row in results:
        step = row["step"]
        after = row.get("after") or {}
        contacts = row.get("contact_events")
        if not isinstance(contacts, int):
            failures.append(f"{step}: contact monitoring was unavailable")
        elif contacts:
            failures.append(f"{step}: {contacts} contact event(s) with world-box geometry")
        if step.startswith("roller:"):
            if row["ok"] or "on skates" not in (row.get("reason") or ""):
                failures.append(f"{step}: expected an explicit refusal of skating navigation")
        elif not row["ok"]:
            failures.append(f"{step}: {row.get('reason') or 'step failed'}")
        elif "final_m" in row:
            distance = row["final_m"]
            if (
                not isinstance(distance, (int, float))
                or isinstance(distance, bool)
                or not math.isfinite(distance)
                or distance < 0
            ):
                failures.append(f"{step}: no measurable final distance")
            elif distance > 0.20:
                failures.append(f"{step}: arrival was {distance:.3f} m from the target")
            if not after.get("converged"):
                failures.append(f"{step}: position was not confirmed after stopping")
        max_converged = row.get("max_converged_m")
        if max_converged is not None:
            if (
                not isinstance(max_converged, (int, float))
                or isinstance(max_converged, bool)
                or not math.isfinite(max_converged)
                or max_converged < 0
            ):
                failures.append(f"{step}: confirmed-position error was not measurable")
            elif max_converged > 0.20:
                failures.append(f"{step}: a confirmed position was {max_converged:.3f} m wrong")
        if after.get("converged"):
            error = after.get("err")
            if (
                not isinstance(error, (int, float))
                or isinstance(error, bool)
                or not math.isfinite(error)
                or error < 0
            ):
                failures.append(f"{step}: final confirmed-position error was not measurable")
            elif error > 0.20:
                failures.append(f"{step}: a confirmed position was {error:.3f} m wrong")
    return failures


def main(args: argparse.Namespace) -> int:
    scene_dir = apartment_dir()
    argv = [
        "--runtime",
        str(STREAM / "microduck"),
        "--rl",
        str(STREAM / "microduck_rl"),
        "--scene",
        str(scene_dir / "scene_appartement.xml"),
        "--preload-equipment",
        "--places",
        str(scene_dir / "navigation_appartement.json"),
        "--start",
        args.start,
        "--link",
        "webrtc",
        "--realism",
        args.realism,
        "--realism-seed",
        str(args.seed),
        "--headless",
    ]
    launch = live_stack.parse_args(argv)
    env = live_stack.environment(launch)
    occupied = [port for port in live_stack.PORTS if live_stack.listening(port)]
    if occupied or live_stack.LOCK.exists():
        print(f"live stack busy (ports {occupied}, lock {live_stack.LOCK.exists()})")
        return 2
    live_stack.LOCK.mkdir(parents=True)
    state = Path(tempfile.mkdtemp(prefix="cc-repro-", dir=Path.home() / ".cache"))
    env["DUCK_SIM_STATE"] = str(state)
    contact_dir = state / "contact_monitor"
    contact_dir.mkdir()
    (contact_dir / "sitecustomize.py").write_text(
        "import sys\n"
        "if 'mjlab_microduck.sim.body_server' in sys.orig_argv:\n"
        f"    exec({CONTACT_BOOTSTRAP!r}, globals())\n"
    )
    env["DUCK_DIAG_CONTACT_LOG"] = str(state / "contacts.jsonl")
    env["PYTHONPATH"] = os.pathsep.join(
        path for path in (str(contact_dir), env.get("PYTHONPATH", "")) if path
    )
    sim = STREAM / "microduck" / "scripts" / "duck-sim"
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.with_suffix("").mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["setpriv", "--no-new-privs", str(sim)], cwd=COIN, env=env, check=True)
        with out_path.open("w") as out:
            results = asyncio.run(run_steps(args, state, out))
        shutil.copyfile(state / "contacts.jsonl", out_path.with_suffix("") / "contacts.jsonl")
    finally:
        subprocess.run(
            ["setpriv", "--no-new-privs", str(sim), "down"], cwd=COIN, env=env, timeout=60
        )
        live_stack.LOCK.rmdir()
        print(f"state: {state}")
    if args.expect_free_route:
        failures = free_route_failures(results)
        for failure in failures:
            print(f"FREE ROUTE FAILED: {failure}", file=sys.stderr)
        print("Contacts are measured in the isolated simulator, not supplied by WebRTC.")
        if failures:
            return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--realism", default="real")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--start", default="départ@0")
    parser.add_argument("--no-auto-equipment", dest="auto_equipment", action="store_false")
    parser.add_argument("--out", default="logs/nav_repro/live.jsonl")
    parser.add_argument("--record", action="store_true", help="save every ToF frame and state")
    parser.add_argument(
        "--expect-free-route",
        action="store_true",
        help="fail on unexpected refusals or walking arrivals more than 0.20 m from the target",
    )
    parser.add_argument("steps", nargs="*", default=["mode:roller", "mode:walk", "ma chambre"])
    raise SystemExit(main(parser.parse_args()))
