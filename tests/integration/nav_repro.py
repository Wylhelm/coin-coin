"""Replay a spoken navigation session headless and log the estimate against the truth.

    flock -w 1800 /tmp/microduck-sim-agents.lock .venv/bin/python -m tests.integration.nav_repro \\
        --realism real --seed 0 --prelude --out logs/nav_repro/real0.jsonl salon "ma chambre" départ

`--prelude` does what the user did first: move forward 0.25 m/s for 5 s, then turn 180 degrees.
`--start ''` drops the prior: the duck must find itself globally before going anywhere.
`--mode roller` sends go_to in skates, which needs glide odometry to be honest.
Every 0.25 s a JSON line records the phase, the true pose (scoring only), odometry yaw and the
estimate. The controller never reads the truth.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from pathlib import Path
from typing import Any

import numpy as np

from coincoin.clock import SystemClock
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.robot.governor import Governor, ImmediateToolResult
from coincoin.robot.localization import EstimatedPose, TcpTofStream
from coincoin.robot.navigation import NavigationMap, Navigator, Pose, SimPose
from tests.integration import sim_stack as sim_stack_module
from tests.integration.sim_stack import SimStack, apartment_dir, sim_stack


def _wrap(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


class Trace:
    def __init__(self, stack: SimStack, localizer: EstimatedPose, out: Any) -> None:
        self.stack, self.localizer, self.out = stack, localizer, out
        self.phase = "idle"
        self.start = time.monotonic()
        self.rows: dict[str, list[dict[str, Any]]] = {}

    async def sample(self) -> dict[str, Any]:
        probe = await self.stack.probe()
        state = self.stack.link.state() or {}
        odom = state.get("odom") or {}
        odom_pose = self.localizer.latest_odom()
        estimate = self.localizer.current()
        row: dict[str, Any] = {
            "t": round(time.monotonic() - self.start, 2),
            "phase": self.phase,
            "true": [round(probe["position"][0], 3), round(probe["position"][1], 3)],
            "true_yaw": round(math.degrees(probe["yaw"]), 1),
            "odom_yaw": round(math.degrees(odom.get("yaw", math.nan)), 1),
            "odom_xy": [odom_pose.x, odom_pose.y] if odom_pose is not None else None,
            "contacts": probe["contact_count"],
            "height": round(probe["height"], 3),
            "roll": round(probe["roll"], 1),
            "pitch": round(probe["pitch"], 1),
        }
        if estimate.std_xy < math.inf:
            row.update(
                est=[round(estimate.x, 3), round(estimate.y, 3)],
                est_yaw=round(math.degrees(estimate.yaw), 1),
                std_xy=round(estimate.std_xy, 3),
                std_yaw=round(math.degrees(estimate.std_yaw), 1),
                fit=round(estimate.fit, 2),
                converged=estimate.converged,
                err=math.dist(probe["position"], (estimate.x, estimate.y)),
                yaw_err=round(math.degrees(_wrap(probe["yaw"] - estimate.yaw)), 1),
            )
        filt = getattr(self.localizer, "filter", None)
        if filt is not None:
            row["blind"] = round(filt.blind_s, 2)
            row["bel"] = round(float(np.mean(filt.speed)), 2)
        applied = (state.get("move") or {}).get("applied")
        if isinstance(applied, (list, tuple)) and applied:
            row["cmd"] = round(float(applied[0]), 2)
        scan = self.localizer.latest_scan()
        if scan is not None:
            row["scan_t_ns"] = scan.t_ns
        if scan is not None and scan.t_ns != getattr(self, "_scan_t", None):
            self._scan_t = scan.t_ns
            low = scan.hits[scan.hits[:, 2] <= 0.35]
            truth = Pose(probe["position"][0], probe["position"][1], probe["yaw"])
            for name, pose in (("truth", truth), ("est", estimate.pose)):
                if not math.isfinite(pose.x):
                    continue
                c, s = math.cos(pose.yaw), math.sin(pose.yaw)
                wx = pose.x + c * low[:, 0] - s * low[:, 1]
                wy = pose.y + s * low[:, 0] + c * low[:, 1]
                odd = self.localizer.field.lookup(wx, wy) > 0.15
                row[f"unexpl_{name}"] = int(odd.sum())
                if name == "truth" and odd.any():
                    row["unexpl_truth_pts"] = [
                        [round(float(h[0]), 2), round(float(h[1]), 2), round(float(h[2]), 3)]
                        for h in low[odd][:6]
                    ]
            row["low_hits"] = len(low)
            pts = np.vstack([scan.hits, scan.floor]) if len(scan.floor) else scan.hits
            c, s = math.cos(truth.yaw), math.sin(truth.yaw)
            wx = truth.x + c * pts[:, 0] - s * pts[:, 1]
            wy = truth.y + s * pts[:, 0] + c * pts[:, 1]
            ground = pts[self.localizer.field.lookup(wx, wy) > 0.15]
            if len(ground) >= 8:
                design = np.column_stack((ground[:, 0], ground[:, 1], np.ones(len(ground))))
                (a, b, z0), *_ = np.linalg.lstsq(design, ground[:, 2], rcond=None)
                row["floor_fit"] = [
                    round(math.degrees(math.atan(a)), 2),
                    round(math.degrees(math.atan(b)), 2),
                    round(float(z0), 3),
                    len(ground),
                ]
        return row

    async def run(self) -> None:
        while True:
            row = await self.sample()
            self.rows.setdefault(self.phase, []).append(row)
            self.out.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.out.flush()
            await asyncio.sleep(0.25)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    errors = [row["err"] for row in rows if "err" in row]
    converged_errors = [row["err"] for row in rows if row.get("converged") and "err" in row]
    yaws = [abs(row["yaw_err"]) for row in rows if "yaw_err" in row]
    if not errors:
        return {}
    return {
        "rms_m": round(math.sqrt(sum(e * e for e in errors) / len(errors)), 3),
        "max_m": round(max(errors), 3),
        "max_converged_m": max(converged_errors, default=None),
        "yaw_rms_deg": round(math.sqrt(sum(y * y for y in yaws) / len(yaws)), 1),
        "yaw_max_deg": round(max(yaws), 1),
        "contacts": (
            None
            if any(row.get("contacts") is None for row in rows)
            else sum(1 for row in rows if row["contacts"])
        ),
    }


def read_contact_events(path: Path) -> int:
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if not rows or rows[0] != {"ready": True}:
        raise RuntimeError("the isolated simulator did not enable contact monitoring")
    for expected, row in enumerate(rows[1:], 1):
        if row.get("event") != expected:
            raise RuntimeError("the contact monitor produced an incomplete event log")
    return len(rows) - 1


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


def _prepare(out: str) -> tuple[Path, Path]:
    out_path = Path(out).resolve()
    work = out_path.with_suffix("")
    work.mkdir(parents=True, exist_ok=True)
    return out_path, work


async def main(args: argparse.Namespace) -> list[dict[str, Any]]:
    os.environ["DUCK_SIM_REALISM"] = args.realism
    os.environ["DUCK_SIM_REALISM_SEED"] = str(args.seed)
    if args.overrides:
        # Diagnosis only: switch single realism effects off to see which one matters.
        sim_stack_module.BODY_BOOTSTRAP = (
            "import dataclasses, json\n"
            "from mjlab_microduck.sim import realism\n"
            f"realism.PROFILES[{args.realism!r}] = dataclasses.replace("
            f"realism.PROFILES[{args.realism!r}], **json.loads({args.overrides!r}))\n"
            + sim_stack_module.BODY_BOOTSTRAP
        )
    out_path, work = _prepare(args.out)
    nav_map = NavigationMap.load(apartment_dir() / "navigation_appartement.json")
    results: list[dict[str, Any]] = []
    with out_path.open("w") as out:
        async with sim_stack(work, apartment_dir() / "scene_appartement.xml") as stack:
            localizer = EstimatedPose(
                nav_map, stack.link.state, TcpTofStream(stack.tof_url), clock=SystemClock().now
            )
            if args.start:
                localizer.start_at(args.start)
            localizer.start()
            navigator = Navigator(
                nav_map, localizer, oracle=SimPose(stack.body_url), localizer=localizer
            )
            governor = Governor(
                stack.link,
                Settings(max_move_s=30, walk_assist=True),
                SystemClock(),
                Journal(work / "journal"),
                navigator=navigator,
            )
            await governor.start()
            trace = Trace(stack, localizer, out)
            tracer = asyncio.create_task(trace.run())
            trace.phase = "stand"
            await asyncio.sleep(4.0)
            try:
                steps: list[tuple[str, Any]] = []
                if args.prelude:
                    steps += [
                        ("move", lambda: governor.move(0.25, 0, 0, 5)),
                        ("turn", lambda: governor.turn(180)),
                    ]
                for place in args.places:
                    steps.append((place, lambda place=place: governor.go_to(place, mode=args.mode)))
                for name, call in steps:
                    samples_before = len(trace.rows.get(name, []))
                    trace.phase = name
                    began = time.monotonic()
                    started = await call()
                    result = (
                        started
                        if not started.ok or isinstance(started, ImmediateToolResult)
                        else await governor.wait_motion()
                    )
                    trace.phase = f"{name}:settle"
                    await asyncio.sleep(2.0)
                    row = await trace.sample()
                    summary = {
                        "step": name,
                        "ok": bool(result and result.ok),
                        "detail": result.detail if result else None,
                        "reason": result.reason if result else None,
                        "seconds": round(time.monotonic() - began, 1),
                        **summarize(trace.rows.get(name, [])[samples_before:]),
                        "after": row,
                    }
                    if name in navigator.places:
                        final = await stack.probe()
                        summary["final_m"] = math.dist(final["position"], navigator.places[name])
                    results.append(summary)
                    print(json.dumps(summary, ensure_ascii=False), flush=True)
                    out.write(json.dumps({"summary": summary}, ensure_ascii=False) + "\n")
                    if name in navigator.places and not summary["ok"] and args.stop_on_failure:
                        break
            finally:
                tracer.cancel()
                await governor.close()
                await navigator.close()
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--realism", default="off")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--prelude", action="store_true")
    parser.add_argument("--stop-on-failure", action="store_true")
    parser.add_argument("--overrides", default="", help="JSON realism profile field overrides")
    parser.add_argument(
        "--start",
        default="départ@0",
        help="start_at prior; pass an empty string for an unknown start (global relocalization)",
    )
    parser.add_argument("--mode", default="walk", choices=["walk", "roller"])
    parser.add_argument("--out", default="logs/nav_repro/run.jsonl")
    parser.add_argument("places", nargs="*", default=["salon", "ma chambre", "départ"])
    asyncio.run(main(parser.parse_args()))
