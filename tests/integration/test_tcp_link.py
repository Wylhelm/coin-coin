import asyncio
import json
import math
from pathlib import Path

import pytest

from coincoin.clock import FakeClock
from coincoin.robot.frames import FrameError
from coincoin.robot.jsonrpc import API_VERSION, JsonRpcConnection, JsonRpcError
from coincoin.robot.link import LinkDown, RobotLink, RobotRefused
from coincoin.robot.tcp_link import TcpLink, tcp_address
from relay.duck_relay import DuckRelay
from tests.fakes.fake_robotd import FakeRobotd


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():  # noqa: ASYNC110 - bounded observation of network callbacks
            await asyncio.sleep(0.005)


@pytest.fixture
async def robot():
    daemon = FakeRobotd()
    await daemon.start()
    yield daemon
    await daemon.close_server()


async def test_hello_subscribe_state_concurrent_requests_and_notify(robot):
    link = TcpLink(f"tcp://127.0.0.1:{robot.port}")
    assert isinstance(link, RobotLink)
    assert link.state() is None and math.isinf(link.state_age())
    await link.connect()
    await link.connect()
    await until(lambda: link.state() is not None)
    assert link.connected and link.state_age() < 1
    results = await asyncio.gather(*(link.call("robot.health") for _ in range(20)))
    assert results == [{"healthy": True}] * 20
    await link.notify("robot.move", {"vx": 0.1, "vy": 0, "vyaw": 0})
    await until(lambda: any(record.method == "robot.move" for record in robot.records))
    assert [record.method for record in robot.records[:3]] == [
        "hello",
        "robot.subscribe",
        "robot.mouth",
    ]
    assert robot.records[0].params == {"api_version": API_VERSION}
    assert robot.records[1].params == {"hz": 10}
    assert robot.records[-1].notification
    robot.refusals["robot.do"] = "precise daemon reason"
    with pytest.raises(RobotRefused, match="precise daemon reason"):
        await link.call("robot.do", {"skill": "roulade"})
    await link.close()
    assert not link.connected and link.state() is None
    with pytest.raises(LinkDown):
        await link.call("robot.health")
    with pytest.raises(LinkDown):
        await link.notify("robot.stop")
    with pytest.raises(FrameError):
        await link.frame()


async def test_first_line_authentication_and_relay_error(tmp_path: Path):
    daemon = FakeRobotd()
    await daemon.start(unix=tmp_path / "duck-a.sock")
    relay = DuckRelay(tmp_path, robot_port=0, frame_port=0, token="offline-only-token")
    await relay.start()
    port = relay.servers[0].sockets[0].getsockname()[1]
    bad = TcpLink(f"tcp://127.0.0.1:{port}", token="incorrect")
    with pytest.raises(LinkDown):
        await bad.connect()
    await bad.close()
    link = TcpLink(f"tcp://127.0.0.1:{port}", token="offline-only-token")
    try:
        await link.connect()
        with pytest.raises(JsonRpcError) as failure:
            await link.call("robot.relax")
        assert failure.value.code == -32601
        assert not any(record.method == "robot.relax" for record in daemon.records)
    finally:
        await link.close()
        await relay.close()
        await daemon.close_server()


async def test_reconnect_clears_state_and_runs_callbacks(robot):
    clock = FakeClock()
    events = []

    async def on_up():
        events.append("up")

    link = TcpLink(
        f"tcp://127.0.0.1:{robot.port}",
        clock=clock,
        on_up=on_up,
        on_down=lambda: events.append("down"),
    )
    await link.connect()
    await until(lambda: link.state() is not None)
    await robot.disconnect_clients()
    await until(lambda: not link.connected and events[-1] == "down")
    assert link.state() is None and math.isinf(link.state_age())
    clock.advance(0.61)
    await until(lambda: link.connected)
    await until(lambda: link.state() is not None)
    assert events == ["up", "down", "up"]
    assert len([record for record in robot.records if record.method == "hello"]) == 2
    await link.close()
    assert events[-1] == "down"


class BufferWriter:
    def __init__(self):
        self.wire = []
        self.block = False
        self.failed = False
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.failed:
            raise OSError("scripted write error")
        self.wire.append(json.loads(data))

    async def drain(self) -> None:
        if self.block:
            await asyncio.Event().wait()

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return


async def test_rpc_correlation_timeouts_late_responses_and_hello_error():
    reader = asyncio.StreamReader()
    writer = BufferWriter()
    connection = JsonRpcConnection(reader, writer)
    first = asyncio.create_task(connection.call("first"))
    second = asyncio.create_task(connection.call("second"))
    await until(lambda: len(writer.wire) == 2)
    reader.feed_data(b'{"jsonrpc":"2.0","id":2,"result":{"second":true}}\n')
    reader.feed_data(
        b'{"jsonrpc":"2.0","method":"robot.state","params":{"safety":{"fallen":false}}}\n'
    )
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"old hello"}}\n')
    assert await second == {"second": True}
    with pytest.raises(JsonRpcError) as failure:
        await first
    assert failure.value.code == -32601
    assert connection.state() is not None
    with pytest.raises(TimeoutError):
        await connection.call("timeout", timeout=0.01)
    reader.feed_data(b'{"jsonrpc":"2.0","id":3,"result":{}}\n')
    assert connection.connected
    await connection.close()


@pytest.mark.parametrize("fault", ["blocked", "write_error", "locked"])
async def test_notify_is_bounded_to_fifty_ms(fault):
    reader = asyncio.StreamReader()
    writer = BufferWriter()
    connection = JsonRpcConnection(reader, writer)
    writer.block = fault in ("blocked", "locked")
    writer.failed = fault == "write_error"
    pending = None
    if fault == "locked":
        pending = asyncio.create_task(connection.call("blocked", timeout=1))
        await until(lambda: bool(writer.wire))
    start = asyncio.get_running_loop().time()
    with pytest.raises(LinkDown):
        await connection.notify("robot.stop")
    assert asyncio.get_running_loop().time() - start < 0.12
    assert not connection.connected and writer.closed
    await connection.close()
    if pending is not None:
        with pytest.raises(LinkDown):
            await pending


@pytest.mark.parametrize(
    "line",
    [
        b"bad-json\n",
        b"[]\n",
        b'{"jsonrpc":"1.0"}\n',
        b'{"jsonrpc":"2.0","method":"robot.state","params":[]}\n',
        b'{"jsonrpc":"2.0","id":1,"result":NaN}\n',
        b'{"jsonrpc":"2.0","id":1,"result":1e999}\n',
        b'{"jsonrpc":"2.0","id":1,"error":{"code":"bad"}}\n',
        b'{"jsonrpc":"2.0","id":1}\n',
        b"x" * 65538 + b"\n",
    ],
)
async def test_bad_wire_drops_link_and_pending_call(line):
    reader = asyncio.StreamReader()
    writer = BufferWriter()
    connection = JsonRpcConnection(reader, writer)
    pending = asyncio.create_task(connection.call("health"))
    await until(lambda: bool(writer.wire))
    reader.feed_data(line)
    with pytest.raises(LinkDown):
        await pending
    assert not connection.connected
    await connection.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:1",
        "tcp://localhost",
        "tcp://u:p@localhost:2",
        "tcp://localhost:0",
        "tcp://localhost:65536",
        "tcp://localhost:2/path",
        "tcp://localhost:2?a=b",
        "tcp://localhost:2#fragment",
    ],
)
def test_tcp_url_validation(url):
    with pytest.raises(ValueError):
        tcp_address(url)


def test_tcp_token_and_optional_frame_url():
    assert tcp_address("tcp://[::1]:7830") == ("::1", 7830)
    with pytest.raises(ValueError):
        TcpLink("tcp://127.0.0.1:7830", token="bad\n")
    link = TcpLink("tcp://127.0.0.1:7830", frame_url="tcp://127.0.0.1:7831")
    assert not link.connected


async def test_initial_failure_recovers_with_backoff_and_connect_waiter(robot):
    port = robot.port
    await robot.close_server()
    clock = FakeClock()
    link = TcpLink(f"tcp://127.0.0.1:{port}", clock=clock)
    with pytest.raises(LinkDown):
        await link.connect()
    await robot.start(port=port)
    waiter = asyncio.create_task(link.connect())
    await asyncio.sleep(0.01)
    clock.advance(0.61)
    await waiter
    assert link.connected
    await link.close()
