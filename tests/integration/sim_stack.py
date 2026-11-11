"""Launch the real simulator stack for opt-in tests: body server, robotd, tofd and duck-relay.

Shared by the opt-in navigation tests. Callers hold the global simulator lock themselves (see
docs/localization.md). The body server gains two test-only operations: `test_place` teleports the
duck, `test_probe` reports its true pose and world-box contacts, including cumulative physics-step
counts. Only tests read them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coincoin.robot.tcp_link import TcpLink
from relay.duck_relay import DuckRelay

COIN = Path(__file__).resolve().parents[2]
STREAM = COIN.parent


def apartment_dir() -> Path:
    """The scanned apartment: DUCK_APARTMENT_DIR, or the nearest scan_tout_lappart above."""
    if configured := os.environ.get("DUCK_APARTMENT_DIR"):
        return Path(configured).expanduser()
    for parent in COIN.parents:
        if (parent / "scan_tout_lappart").is_dir():
            return parent / "scan_tout_lappart"
    return STREAM / "scan_tout_lappart"


CONTACT_BOOTSTRAP = """
import json
import os
import mujoco

def box_contacts(model, data):
    contacts = []
    for contact in data.contact[:data.ncon]:
        a, b = contact.geom1, contact.geom2
        if model.geom_bodyid[b] == 0:
            a, b = b, a
        if (model.geom_bodyid[a] == 0 and model.geom_bodyid[b] != 0
                and model.geom_type[a] == mujoco.mjtGeom.mjGEOM_BOX):
            contacts.append((int(a), int(b), float(contact.dist)))
    return contacts

contact_steps = 0
contact_events = 0
contact_active = False
contact_details = []
contact_log = os.environ.get("DUCK_DIAG_CONTACT_LOG")
if contact_log:
    with open(contact_log, "w") as out:
        out.write(json.dumps({"ready": True}) + "\\n")
native_step = mujoco.mj_step
def monitored_step(model, data, *args, **kwargs):
    global contact_steps, contact_events, contact_active
    result = native_step(model, data, *args, **kwargs)
    contacts = box_contacts(model, data)
    active = bool(contacts)
    contact_steps += int(active)
    contact_events += int(active and not contact_active)
    if active and not contact_active:
        world, robot, distance = contacts[0]
        contact_details.append({
            "event": contact_events,
            "time_s": float(data.time),
            "position": [float(x) for x in data.qpos[:2]],
            "world_geom": world,
            "world_position": [float(x) for x in model.geom_pos[world]],
            "world_size": [float(x) for x in model.geom_size[world]],
            "robot_geom": mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, robot),
            "robot_body": mujoco.mj_id2name(
                model, mujoco.mjtObj.mjOBJ_BODY, int(model.geom_bodyid[robot])
            ),
            "min_distance": distance,
        })
        if contact_log:
            with open(contact_log, "a") as out:
                out.write(json.dumps(contact_details[-1]) + "\\n")
    elif active:
        contact_details[-1]["min_distance"] = min(
            contact_details[-1]["min_distance"], *(d for _, _, d in contacts)
        )
    contact_active = active
    return result
mujoco.mj_step = monitored_step
"""

BODY_BOOTSTRAP = (
    """
import math
from mjlab_microduck.sim import body_server
"""
    + CONTACT_BOOTSTRAP
    + """
dispatch = body_server.Handler.dispatch
def with_test_operations(self, body, request):
    if request.get("op") == "test_place":
        with body.world.lock:
            data = body.world.data
            data.qpos[body.trunk : body.trunk + 2] = request["position"]
            yaw = request["yaw"]
            data.qpos[body.trunk + 3 : body.trunk + 7] = [
                math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)
            ]
            data.qvel[body.trunk_dof : body.trunk_dof + 6] = 0
            mujoco.mj_forward(body.world.model, data)
        return {}
    if request.get("op") == "test_probe":
        with body.world.lock:
            model, data = body.world.model, body.world.data
            contacts = box_contacts(model, data)
            qw, qx, qy, qz = (float(v) for v in data.qpos[body.trunk + 3 : body.trunk + 7])
            return {
                "position": [float(x) for x in data.qpos[body.trunk : body.trunk + 2]],
                "height": float(data.qpos[body.trunk + 2]),
                "roll": math.degrees(
                    math.atan2(2 * (qw * qx + qy * qz), 1 - 2 * (qx**2 + qy**2))
                ),
                "pitch": math.degrees(math.asin(max(-1.0, min(1.0, 2 * (qw * qy - qz * qx))))),
                "yaw": float(
                    math.atan2(
                        2 * (qw * qz + qx * qy),
                        1 - 2 * (qy**2 + qz**2),
                    )
                ),
                "contact_count": len(contacts),
                "penetration": max([0.0, *(-d for _, _, d in contacts)]),
                "contact_steps": contact_steps,
                "contact_events": contact_events,
                "contact_details": contact_details[-20:],
                "contact_step_s": float(model.opt.timestep),
            }
    return dispatch(self, body, request)
body_server.Handler.dispatch = with_test_operations
body_server.main()
"""
)


async def body_request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, request: dict[str, Any]
) -> dict[str, Any]:
    writer.write((json.dumps(request) + "\n").encode())
    await writer.drain()
    async with asyncio.timeout(3):
        response = json.loads(await reader.readline())
    assert "error" not in response, response
    return response


@dataclass
class SimStack:
    tmp: Path
    body_port: int
    link: TcpLink
    relay: DuckRelay
    tof_url: str
    probe_reader: asyncio.StreamReader
    probe_writer: asyncio.StreamWriter
    # One request at a time: the trace sampler and the governor both probe the same pipe.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def body_url(self) -> str:
        return f"tcp://127.0.0.1:{self.body_port}"

    async def request(self, request: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            return await body_request(self.probe_reader, self.probe_writer, request)

    async def probe(self) -> dict[str, Any]:
        return await self.request({"op": "test_probe"})

    async def place(self, position: list[float], yaw: float) -> None:
        await self.request({"op": "test_place", "position": position, "yaw": yaw})

    async def read(self) -> dict[str, Any]:
        return await self.request({"op": "read"})


@contextlib.asynccontextmanager
async def sim_stack(tmp_path: Path, scene: Path, *, tof: bool = True) -> AsyncIterator[SimStack]:
    runtime, rl = STREAM / "microduck", STREAM / "microduck_rl"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    params = tmp_path / "robotd.toml"
    params.write_text(
        f'[policy]\ndirectory = "{runtime / "policies"}"\n'
        "[audio]\nenabled = false\ngreet = false\n[pickup]\nenabled = false\n"
    )
    env = os.environ.copy()
    env["ORT_DYLIB_PATH"] = str(
        next((rl / ".venv").glob("lib/python*/site-packages/onnxruntime/capi/libonnxruntime.so*"))
    )
    env["DUCK_RUNTIME_DIR"] = str(tmp_path)
    processes: list[asyncio.subprocess.Process] = []
    logs = []
    relay: DuckRelay | None = None
    link: TcpLink | None = None
    probe_writer: asyncio.StreamWriter | None = None

    async def launch(name: str, argv: list[str], cwd: Path) -> None:
        log = (tmp_path / f"{name}.log").open("w")
        logs.append(log)
        processes.append(
            await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env, stdout=log, stderr=asyncio.subprocess.STDOUT
            )
        )

    try:
        await launch(
            "body",
            [
                str(rl / ".venv" / "bin" / "python"),
                "-u",
                "-c",
                BODY_BOOTSTRAP,
                "--headless",
                "--port",
                str(port),
                "--scene",
                str(scene),
                "--keyframe",
                "SIT",
                "--preload-equipment",
            ],
            rl,
        )
        await launch(
            "robotd",
            [
                str(runtime / "target" / "debug" / "robotd"),
                "--params",
                str(params),
                "--socket",
                str(tmp_path / "duck-a.sock"),
                "--sim",
                f"127.0.0.1:{port}",
            ],
            runtime,
        )
        async with asyncio.timeout(60):
            while not (tmp_path / "duck-a.sock").exists():
                assert all(process.returncode is None for process in processes)
                await asyncio.sleep(0.1)
        if tof:
            await launch(
                "tofd",
                [
                    str(runtime / "target" / "debug" / "tofd"),
                    "--sim",
                    f"127.0.0.1:{port}",
                    "--socket",
                    str(tmp_path / "duck-a-tof.sock"),
                ],
                runtime,
            )
            async with asyncio.timeout(20):
                while not (tmp_path / "duck-a-tof.sock").exists():
                    assert all(process.returncode is None for process in processes)
                    await asyncio.sleep(0.1)
        relay = DuckRelay(tmp_path, robot_port=0, frame_port=0, tof_port=0 if tof else None)
        await relay.start()
        relay_port = relay.servers[0].sockets[0].getsockname()[1]
        tof_url = f"tcp://127.0.0.1:{relay.servers[2].sockets[0].getsockname()[1]}" if tof else ""
        link = TcpLink(f"tcp://127.0.0.1:{relay_port}")
        await link.connect()
        async with asyncio.timeout(20):
            while link.state() is None:  # noqa: ASYNC110 - bounded network observation
                await asyncio.sleep(0.05)
        await link.call("robot.enable", {"on": True})
        async with asyncio.timeout(30):
            while True:
                policies = await link.call("robot.policies")
                state = link.state()
                if (
                    policies["homed"]
                    and not policies["sitting"]
                    and state is not None
                    and state.get("policy") == "walk"
                ):
                    break
                await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)
        probe_reader, probe_writer = await asyncio.open_connection("127.0.0.1", port)
        yield SimStack(tmp_path, port, link, relay, tof_url, probe_reader, probe_writer)
    finally:
        if probe_writer is not None:
            probe_writer.close()
            with contextlib.suppress(OSError):
                await probe_writer.wait_closed()
        if link is not None:
            await link.close()
        if relay is not None:
            await relay.close()
        for process in reversed(processes):
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=5)
        for log in logs:
            log.close()
