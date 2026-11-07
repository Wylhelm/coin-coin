"""Opt-in rehearsal against the sibling MuJoCo simulator and a built robotd."""

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
from coincoin.robot.tcp_link import TcpLink
from relay.duck_relay import DuckRelay

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.skipif(
    os.environ.get("DUCK_TEST_LOCOMOTION_SIM") != "1",
    reason="requires sibling runtime/RL checkouts, installed policies and a built robotd",
)


BODY_BOOTSTRAP = """
import mujoco
from mjlab_microduck.sim import body_server

dispatch = body_server.Handler.dispatch
def with_fall(self, body, request):
    if request.get("op") == "test_fall":
        with body.world.lock:
            data = body.world.data
            data.qpos[body.trunk + 2] = 0.06
            data.qpos[body.trunk + 3:body.trunk + 7] = [0.70710678, 0.70710678, 0, 0]
            data.qvel[body.trunk_dof:body.trunk_dof + 6] = 0
            mujoco.mj_forward(body.world.model, data)
        return {}
    return dispatch(self, body, request)
body_server.Handler.dispatch = with_fall
body_server.main()
"""


def body_request(port, request):
    with socket.create_connection(("127.0.0.1", port), timeout=2) as stream:
        stream.sendall((json.dumps(request) + "\n").encode())
        with stream.makefile("rb") as frames:
            return json.loads(frames.readline())


def body_read(port):
    return body_request(port, {"op": "read"})


async def test_real_walk_skate_walk_without_support_during_motion(tmp_path):
    runtime, rl = ROOT / "microduck", ROOT / "microduck_rl"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    params = tmp_path / "robotd.toml"
    params.write_text(
        f'[policy]\ndirectory = "{runtime}/policies"\n'
        "[audio]\nenabled = false\ngreet = false\n[pickup]\nenabled = false\n"
    )
    env = os.environ.copy()
    env["ORT_DYLIB_PATH"] = str(
        next((rl / ".venv").glob("lib/python*/site-packages/onnxruntime/capi/libonnxruntime.so*"))
    )
    env["DUCK_RUNTIME_DIR"] = str(tmp_path)
    camera_args = []
    if os.environ.get("DUCK_TEST_LOCOMOTION_CAMERA") == "1":
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            frame_port = reservation.getsockname()[1]
        camera_args = ["--cameras", "a", "--frame-port", str(frame_port)]
    processes, logs = [], []
    relay = link = governor = None
    try:
        for name, argv, cwd in (
            (
                "body",
                [
                    str(rl / ".venv/bin/python"),
                    "-u",
                    "-c",
                    BODY_BOOTSTRAP,
                    "--headless",
                    "--port",
                    str(port),
                    "--scene",
                    str(rl / "src/mjlab_microduck/robot/microduck/scene_backlash.xml"),
                    "--keyframe",
                    "SIT",
                    *camera_args,
                ],
                rl,
            ),
            (
                "robotd",
                [
                    str(runtime / "target/debug/robotd"),
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
        async with asyncio.timeout(15):
            while not (tmp_path / "duck-a.sock").exists():
                assert all(process.returncode is None for process in processes)
                await asyncio.sleep(0.1)
        relay = DuckRelay(tmp_path, robot_port=0, frame_port=0)
        await relay.start()
        relay_port = relay.servers[0].sockets[0].getsockname()[1]
        link = TcpLink(f"tcp://127.0.0.1:{relay_port}")
        await link.connect()
        async with asyncio.timeout(10):
            while True:
                if link.state() is not None:
                    break
                await asyncio.sleep(0.05)
        governor = Governor(
            link, Settings(max_move_s=15), SystemClock(), Journal(tmp_path / "journal")
        )
        await link.call("robot.enable", {"on": True})
        async with asyncio.timeout(10):
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
        await governor.start()
        for mode in ("walk", "roller", "walk", "roller", "walk"):
            switched = await governor.set_mode(mode)
            assert switched.ok, switched
            assert (await link.call("robot.mode"))["mode"] == mode
            async with asyncio.timeout(2):
                while True:
                    if not (await asyncio.to_thread(body_read, port))["supported"]:
                        break
                    await asyncio.sleep(0.05)
            before = await asyncio.to_thread(body_read, port)
            assert before["mode"] == mode
            deadline = asyncio.get_running_loop().time() + (20 if mode == "roller" else 8)
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.25)
                idle = await asyncio.to_thread(body_read, port)
                assert not idle["supported"]
                assert idle["imu"]["gravity"][2] < -0.8, (mode, "idle", idle)
            idle = await asyncio.to_thread(body_read, port)
            assert idle["imu"]["gravity"][2] < -0.8, (mode, "idle", idle)
            before = idle
            started = asyncio.get_running_loop().time()
            moved = await governor.move(0.2, 0, 0, 10 if mode == "roller" else 3)
            assert moved.ok, moved
            completed = await governor.wait_motion()
            assert completed is not None and completed.ok, completed
            after = await asyncio.to_thread(body_read, port)
            elapsed = asyncio.get_running_loop().time() - started
            assert 0.9 <= (after["sim_time"] - before["sim_time"]) / elapsed <= 1.1
            assert not after["supported"]
            assert after["mode"] == mode
            distance = math.dist(before["trunk"][:2], after["trunk"][:2])
            assert distance > 0.02, (mode, distance)
            assert after["trunk_z"] > 0.1
            assert after["imu"]["gravity"][2] < -0.8
            assert (await link.call("robot.health"))["healthy"]
            print(f"{mode}: {distance:.3f} m without support")

        assert (await governor.set_mode("roller")).ok
        await asyncio.sleep(0.6)
        assert not (await asyncio.to_thread(body_read, port))["supported"]
        for mode in ("roller", "walk"):
            await asyncio.to_thread(body_request, port, {"op": "test_fall"})
            async with asyncio.timeout(3):
                while True:
                    state = link.state()
                    if state is not None and state["safety"]["fallen"]:
                        break
                    await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)
            recovered = (
                await governor.stand() if mode == "roller" else await governor.set_mode(mode)
            )
            assert recovered.ok, recovered
            await asyncio.sleep(3)
            before = await asyncio.to_thread(body_read, port)
            assert before["mode"] == mode
            assert not before["supported"]
            assert before["imu"]["gravity"][2] < -0.8
            assert before["trunk_z"] > 0.1
            assert (await governor.move(0.2, 0, 0, 3)).ok
            completed = await governor.wait_motion()
            assert completed is not None and completed.ok, completed
            after = await asyncio.to_thread(body_read, port)
            assert after["imu"]["gravity"][2] < -0.8
            assert math.dist(before["trunk"][:2], after["trunk"][:2]) > 0.02
    finally:
        try:
            if governor is not None:
                await governor.close()
            if link is not None:
                await link.close()
            if relay is not None:
                await relay.close()
        finally:
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
            for name in ("body", "robotd"):
                log = tmp_path / f"{name}.log"
                if log.exists():
                    print(f"{name} log:\n{log.read_text()[-4000:]}")
