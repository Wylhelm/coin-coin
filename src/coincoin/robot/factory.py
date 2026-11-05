"""Select a robot transport from its configured URL scheme."""

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from coincoin.clock import Clock
from coincoin.robot.link import RobotLink
from coincoin.robot.tcp_link import TcpLink
from coincoin.robot.webrtc_link import WebRtcLink


def create_robot_link(
    url: str,
    *,
    frame_url: str | None = None,
    token: str = "",
    clock: Clock | None = None,
    on_up: Callable[[], Awaitable[None] | None] | None = None,
    on_down: Callable[[], Awaitable[None] | None] | None = None,
) -> RobotLink:
    """Create the configured transport, preserving TCP's relay settings."""
    scheme = urlsplit(url).scheme
    if scheme == "webrtc":
        return WebRtcLink(url, clock=clock, on_up=on_up, on_down=on_down)
    if scheme == "tcp":
        return TcpLink(
            url, frame_url=frame_url, token=token, clock=clock, on_up=on_up, on_down=on_down
        )
    raise ValueError("Expected tcp://host:port or webrtc://host:port")
