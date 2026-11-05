import pytest

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
