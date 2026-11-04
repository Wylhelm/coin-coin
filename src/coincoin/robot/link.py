"""Transport-independent robot command, state and camera contracts."""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Frame:
    """JPEG image with dimensions and Unix capture timestamp in seconds."""

    jpeg: bytes
    width: int
    height: int
    captured_at: float


class RobotRefused(Exception):
    """A robot rejection whose original reason must reach the caller."""

    reason: str

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class LinkDown(Exception):
    """The robot transport is unavailable or disconnected."""


@runtime_checkable
class RobotLink(Protocol):
    """Exchange commands and cache pushed robot.state notifications."""

    async def connect(self) -> None:
        """Connect and initialize the transport's state subscription."""
        ...

    async def close(self) -> None:
        """Close the transport and release all owned resources."""
        ...

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 2.0,  # noqa: ASYNC109 - fixed public RobotLink contract
    ) -> Any:
        """Send a request and await its result; propagate refusals/link failures."""
        ...

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a notification without waiting for a response."""
        ...

    def state(self) -> dict[str, Any] | None:
        """Return the last pushed robot.state, or None before the first state."""
        ...

    def state_age(self) -> float:
        """Seconds since the last state; infinity if no state has arrived."""
        ...

    async def frame(self) -> Frame:
        """Capture a JPEG frame, raising an explicit error if unavailable."""
        ...

    @property
    def connected(self) -> bool:
        """Whether the command transport is currently available."""
        ...
