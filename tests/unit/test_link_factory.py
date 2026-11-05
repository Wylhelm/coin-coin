import pytest

from coincoin.clock import FakeClock
from coincoin.robot.factory import create_robot_link
from coincoin.robot.tcp_link import TcpLink
from coincoin.robot.webrtc_link import WebRtcLink


def test_factory_selects_tcp_and_preserves_frame_and_token() -> None:
    link = create_robot_link(
        "tcp://127.0.0.1:7810",
        frame_url="tcp://127.0.0.1:7811",
        token="offline-test-token",
    )
    assert isinstance(link, TcpLink)
    assert link._frame_address == ("127.0.0.1", 7811)
    assert link._token == "offline-test-token"


def test_factory_selects_webrtc_without_relay_credentials() -> None:
    link = create_robot_link(
        "webrtc://127.0.0.1:8443",
        frame_url="tcp://127.0.0.1:7811",
        token="ignored-for-webrtc",
    )
    assert isinstance(link, WebRtcLink)


def test_factory_rejects_unknown_schemes() -> None:
    with pytest.raises(ValueError, match="Expected tcp"):
        create_robot_link("udp://127.0.0.1:8443")


@pytest.mark.parametrize("scheme", ["tcp", "webrtc"])
def test_factory_preserves_clock_and_callbacks(scheme: str) -> None:
    clock = FakeClock()

    def up() -> None:
        pass

    async def down() -> None:
        pass

    link = create_robot_link(f"{scheme}://127.0.0.1:8443", clock=clock, on_up=up, on_down=down)
    assert isinstance(link, (TcpLink, WebRtcLink))
    assert link._clock is clock
    assert link.on_up is up
    assert link.on_down is down
