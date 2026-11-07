"""Opt-in navigation regression against the scanned apartment and real simulator."""

import asyncio
import json
import math
import os
import socket
from pathlib import Path

import pytest

from coincoin.clock import SystemClock
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.robot.governor import Governor
from coincoin.robot.navigation import NavigationMap, Navigator, SimPose
from coincoin.robot.tcp_link import TcpLink
from relay.duck_relay import DuckRelay

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.skipif(
    os.environ.get("DUCK_TEST_NAVIGATION_SIM") != "1",
    reason="requires the scanned apartment, sibling simulator and a built robotd",
)

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
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, request: dict
) -> dict:
    writer.write((json.dumps(request) + "\n").encode())
    await writer.drain()
    async with asyncio.timeout(3):
        response = json.loads(await reader.readline())
    assert "error" not in response, response
    return response


@pytest.mark.parametrize(
    ("position", "yaw", "already_roller", "destination"),
    [
        ([-7.01468, 0.27027], -2.87632, False, "salon"),
        ([-7.08, 0.12], -2.45, False, "salon"),
        ([-7.11, 0.29], 2.8, False, "salon"),
        (None, 0.0, False, "salon"),
        ([-2.16, 0.232], -0.356, True, "salon"),
        ([0.4, 1.35], 1.0, False, "ma chambre"),
    ],
    ids=[
        "reported",
        "offset",
        "opposite-heading",
        "walk-then-skate",
        "stuck-start-stops",
        "salon-to-bedroom",
    ],
)
async def test_room_navigation_on_skates(
    tmp_path: Path,
    position: list[float] | None,
    yaw: float,
    already_roller: bool,
    destination: str,
) -> None:
    runtime, rl = ROOT / "microduck", ROOT / "microduck_rl"
    scene = ROOT / "scan_tout_lappart" / "scene_appartement.xml"
    nav_map = NavigationMap.load(ROOT / "scan_tout_lappart" / "navigation_appartement.json")
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
    processes = []
    logs = []
    relay = link = governor = None
    probe_writer = None
    try:
        for name, argv, cwd in (
            (
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
            ),
            (
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
            ),
        ):
            log = (tmp_path / f"{name}.log").open("w")
            logs.append(log)
            processes.append(
                await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=cwd,
                    env=env,
                    stdout=log,
                    stderr=asyncio.subprocess.STDOUT,
                )
            )
        async with asyncio.timeout(30):
            while not (tmp_path / "duck-a.sock").exists():
                assert all(process.returncode is None for process in processes)
                await asyncio.sleep(0.1)
        relay = DuckRelay(tmp_path, robot_port=0, frame_port=0)
        await relay.start()
        relay_port = relay.servers[0].sockets[0].getsockname()[1]
        link = TcpLink(f"tcp://127.0.0.1:{relay_port}")
        await link.connect()
        async with asyncio.timeout(20):
            while link.state() is None:  # noqa: ASYNC110 - bounded network observation
                await asyncio.sleep(0.05)
        await link.call("robot.enable", {"on": True})
        async with asyncio.timeout(20):
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
        navigator = Navigator(nav_map, SimPose(f"tcp://127.0.0.1:{port}"))
        governor = Governor(
            link,
            Settings(max_move_s=30, walk_assist=True),
            SystemClock(),
            Journal(tmp_path / "journal"),
            navigator=navigator,
        )
        await governor.start()
        if already_roller:
            switched = await governor.set_mode("roller")
            assert switched.ok, switched
            async with asyncio.timeout(10):
                while (  # noqa: ASYNC110 - bounded network observation
                    await body_request(probe_reader, probe_writer, {"op": "read"})
                )["supported"]:
                    await asyncio.sleep(0.1)
        if position is not None:
            await body_request(
                probe_reader,
                probe_writer,
                {"op": "test_place", "position": position, "yaw": yaw},
            )
            if already_roller:
                await asyncio.sleep(0.25)
                placed = await body_request(probe_reader, probe_writer, {"op": "test_probe"})
                assert math.dist(placed["position"], position) < 0.2, placed
        if position is None:
            started = await governor.go_to("ma chambre", mode="walk")
            assert started.ok, started
            async with asyncio.timeout(150):
                result = await governor.wait_motion()
            assert result is not None and result.ok, result
        started = await governor.go_to(destination, mode="roller")
        assert started.ok, started
        samples = []
        waiter = asyncio.create_task(governor.wait_motion())
        await asyncio.sleep(0)
        loop = asyncio.get_running_loop()
        at = loop.time()
        async with asyncio.timeout(150):
            while not waiter.done():
                sample = await body_request(probe_reader, probe_writer, {"op": "test_probe"})
                sample["elapsed"] = loop.time() - at
                sample["clearance"] = nav_map.clearance_m(tuple(sample["position"]))
                samples.append(sample)
                if len(samples) % 12 == 1:
                    print(
                        f"{sample['elapsed']:5.1f}s position={sample['position']} "
                        f"yaw={sample['yaw']:+.2f} clearance={sample['clearance']:.2f} "
                        f"contacts={sample['contact_count']} "
                        f"penetration={sample['penetration']:.3f}",
                        flush=True,
                    )
                await asyncio.sleep(0.25)
        result = await waiter
        longest_contact = contact_start = 0.0
        in_contact = False
        for sample in samples:
            if sample["contact_count"]:
                if not in_contact:
                    contact_start = sample["elapsed"]
                    in_contact = True
                longest_contact = max(longest_contact, sample["elapsed"] - contact_start)
            else:
                in_contact = False
        state = link.state()
        print(
            f"Result: {result}; samples={len(samples)}, "
            f"longest obstacle contact={longest_contact:.1f}s, "
            f"safety={state.get('safety') if state is not None else None}",
            flush=True,
        )
        assert result is not None
        assert samples, "navigation finished without any pose samples"
        if already_roller:
            assert not result.ok and result.reason == f"stuck on the way to {destination}"
            assert samples[-1]["elapsed"] < 30
            await asyncio.sleep(1)
            stopped = await body_request(probe_reader, probe_writer, {"op": "test_probe"})
            assert math.dist(stopped["position"], samples[-1]["position"]) < 0.08
            return
        assert result.ok, result
        assert longest_contact < 2.0
        assert samples[-1]["elapsed"] < 100
        final = samples[-1]["position"]
        assert math.dist(final, nav_map.places[destination]) <= 0.35
    finally:
        if probe_writer is not None:
            probe_writer.close()
            await probe_writer.wait_closed()
        if governor is not None:
            await governor.close()
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
