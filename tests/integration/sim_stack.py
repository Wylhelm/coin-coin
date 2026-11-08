"""Launch the real simulator stack for opt-in tests: body server, robotd, tofd and duck-relay.

Shared by the opt-in navigation tests. Callers hold the global simulator lock themselves (see
docs/localization.md). The body server gains two test-only operations: `test_place` teleports the
duck, `test_probe` reports its true pose and box contacts. Only tests read them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coincoin.robot.tcp_link import TcpLink
from relay.duck_relay import DuckRelay

COIN = Path(__file__).resolve().parents[2]
STREAM = COIN.parent


BODY_BOOTSTRAP = """
import math
import mujoco
from mjlab_microduck.sim import body_server

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
            contacts = []
            for contact in data.contact[:data.ncon]:
                a, b = contact.geom1, contact.geom2
                if model.geom_bodyid[b] == 0:
                    a, b = b, a
                if (model.geom_bodyid[a] == 0 and model.geom_bodyid[b] != 0
                        and model.geom_type[a] == mujoco.mjtGeom.mjGEOM_BOX):
                    contacts.append(float(contact.dist))
            return {
                "position": [float(x) for x in data.qpos[body.trunk : body.trunk + 2]],
                "yaw": float(
                    math.atan2(
                        2 * (data.qpos[body.trunk + 3] * data.qpos[body.trunk + 6]
                             + data.qpos[body.trunk + 4] * data.qpos[body.trunk + 5]),
                        1 - 2 * (data.qpos[body.trunk + 5] ** 2
                                 + data.qpos[body.trunk + 6] ** 2),
                    )
                ),
                "contact_count": len(contacts),
                "penetration": max([0.0, *(-d for d in contacts)]),
            }
    return dispatch(self, body, request)
body_server.Handler.dispatch = with_test_operations
body_server.main()
"""


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

    @property
    def body_url(self) -> str:
        return f"tcp://127.0.0.1:{self.body_port}"

    async def probe(self) -> dict[str, Any]:
        return await body_request(self.probe_reader, self.probe_writer, {"op": "test_probe"})

    async def place(self, position: list[float], yaw: float) -> None:
        await body_request(
            self.probe_reader,
            self.probe_writer,
            {"op": "test_place", "position": position, "yaw": yaw},
        )

    async def read(self) -> dict[str, Any]:
        return await body_request(self.probe_reader, self.probe_writer, {"op": "read"})


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
