import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from coincoin.cli import execute, parser
from coincoin.config import Settings
from relay.duck_relay import DuckRelay
from tests.fakes.fake_robotd import FakeRobotd

RELAY_SCRIPT = Path(__file__).resolve().parents[2] / "relay" / "duck_relay.py"


async def test_standalone_relay_help_without_site_packages():
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        str(RELAY_SCRIPT),
        "--help",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
    assert process.returncode == 0, stderr.decode()
    assert b"--state-dir" in stdout


@pytest.fixture
async def proxy(tmp_path: Path):
    robot = FakeRobotd()
    await robot.start(unix=tmp_path / "duck-a.sock")
    relay = DuckRelay(tmp_path, robot_port=0, frame_port=0, token="test-relay-token")
    await relay.start()
    yield relay, robot
    await relay.close()
    await robot.close_server()


async def connection(relay: DuckRelay, *, token: str = "test-relay-token", frame: bool = False):
    port = relay.servers[int(frame)].sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    if token:
        writer.write(token.encode() + b"\n")
        await writer.drain()
    return reader, writer


async def test_authentication_precedes_unix_connection(proxy, caplog):
    relay, robot = proxy
    caplog.set_level(logging.INFO)
    reader, writer = await connection(relay, token="wrong-private-token")
    assert await asyncio.wait_for(reader.read(), 1) == b""
    assert not robot.clients
    assert "wrong-private-token" not in caplog.text
    writer.close()
    await writer.wait_closed()


async def test_refusal_and_oversized_recovery_keep_connection(proxy):
    relay, robot = proxy
    reader, writer = await connection(relay)
    writer.write(b'{"jsonrpc":"2.0","id":1,"method":"robot.relax"}\n')
    writer.write(b'{"jsonrpc":"2.0","method":"robot.init"}\n')
    writer.write(b"[" + b"x" * 200_000 + b"]\n")
    writer.write(b'[{"jsonrpc":"2.0","method":"hello"}]\n')
    writer.write(b'{"jsonrpc":"2.0","id":2,"method":"hello","params":{"api_version":47}}\n')
    await writer.drain()
    replies = [json.loads(await asyncio.wait_for(reader.readline(), 2)) for _ in range(4)]
    assert replies[0]["error"]["code"] == -32601
    assert [reply["error"]["code"] for reply in replies[1:3]] == [-32600, -32600]
    assert replies[3]["id"] == 2 and replies[3]["result"]["api_version"] == 47
    assert [record.method for record in robot.records] == ["hello"]
    writer.close()
    await writer.wait_closed()


async def test_cli_raw_tcp_keeps_relay_refusal_and_permitted_calls(
    proxy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    relay, robot = proxy
    monkeypatch.chdir(tmp_path)
    port = relay.servers[0].sockets[0].getsockname()[1]
    settings = Settings(
        robot_link=f"tcp://127.0.0.1:{port}", relay_token=SecretStr("test-relay-token")
    )
    output, code = await execute(parser().parse_args(["raw", "robot.init"]), settings)
    assert code == 1
    assert output == {"ok": False, "code": -32601, "reason": "refusee par duck-relay"}
    assert not any(record.method == "robot.init" for record in robot.records)
    output, code = await execute(parser().parse_args(["raw", "robot.health"]), settings)
    assert code == 0 and output["ok"] and output["result"]["healthy"]
    assert any(record.method == "robot.health" for record in robot.records)


async def test_server_states_and_replies_share_connection(proxy):
    relay, _ = proxy
    reader, writer = await connection(relay)
    writer.write(b'{"jsonrpc":"2.0","id":1,"method":"robot.subscribe","params":{"hz":10}}\n')
    await writer.drain()
    reply = json.loads(await reader.readline())
    state = json.loads(await reader.readline())
    assert reply["id"] == 1
    assert state["method"] == "robot.state"
    writer.write(b'{"jsonrpc":"2.0","id":2,"method":"robot.health"}\n')
    await writer.drain()
    async with asyncio.timeout(1):
        while (reply := json.loads(await reader.readline())).get("id") != 2:
            assert reply["method"] == "robot.state"
    assert reply["result"]["healthy"]
    writer.close()
    await writer.wait_closed()


async def test_frame_binary_response_is_untouched(tmp_path: Path):
    binary = bytes(range(256)) * 800
    response = b'{"jsonrpc":"2.0","id":1,"result":{"bytes":204800}}\n' + binary
    seen = []
    done = asyncio.Event()

    async def camera(reader, writer):
        try:
            seen.append(await reader.readline())
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            done.set()

    server = await asyncio.start_unix_server(camera, tmp_path / "duck-a-frame.sock")
    relay = DuckRelay(tmp_path, robot_port=0, frame_port=0)
    await relay.start()
    reader, writer = await connection(relay, token="", frame=True)
    request = b'{"jsonrpc":"2.0","id":1,"method":"media.frame"}\n'
    writer.write(request)
    await writer.drain()
    assert await asyncio.wait_for(reader.readexactly(len(response)), 2) == response
    assert seen == [request]
    await done.wait()
    writer.close()
    await writer.wait_closed()
    await relay.close()
    server.close()
    await server.wait_closed()


async def test_missing_target_shutdown_and_incomplete_line(tmp_path: Path, caplog):
    caplog.set_level(logging.INFO)
    relay = DuckRelay(tmp_path, robot_port=0, frame_port=0)
    await relay.start()
    reader, writer = await connection(relay, token="")
    assert await reader.read() == b""
    assert "Unix target unavailable" in caplog.text
    writer.close()
    await writer.wait_closed()
    await relay.close()
    robot = FakeRobotd()
    await robot.start(unix=tmp_path / "duck-a.sock")
    await relay.start()
    port = relay.servers[0].sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"unterminated")
    await writer.drain()
    writer.write_eof()
    assert json.loads(await reader.readline())["error"]["code"] == -32600
    writer.close()
    await writer.wait_closed()
    await relay.close()
    await robot.close_server()


async def test_relay_close_disconnects_active_clients(proxy):
    relay, _ = proxy
    reader, writer = await connection(relay)
    writer.write(b'{"jsonrpc":"2.0","id":1,"method":"hello"}\n')
    await writer.drain()
    await reader.readline()
    await relay.close()
    assert await asyncio.wait_for(reader.read(), 1) == b""
    writer.close()
    await writer.wait_closed()


async def test_start_failure_closes_first_listener(tmp_path: Path):
    occupied = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = occupied.sockets[0].getsockname()[1]
    relay = DuckRelay(tmp_path, robot_port=0, frame_port=port)
    with pytest.raises(OSError):
        await relay.start()
    assert not relay.servers
    occupied.close()
    await occupied.wait_closed()


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
async def test_standalone_relay_exits_cleanly_on_signal(tmp_path: Path, signum):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(RELAY_SCRIPT),
        "--state-dir",
        str(tmp_path),
        "--robot-port",
        "0",
        "--frame-port",
        "0",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stderr is not None
        async with asyncio.timeout(3):
            assert b"listening" in await process.stderr.readline()
            assert b"listening" in await process.stderr.readline()
            process.send_signal(signum)
            await process.communicate()
        assert process.returncode == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
