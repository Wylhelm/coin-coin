"""What the connected robot actually allows, probed once per connection.

Nothing here assumes the transport: the same robot answers differently over the local TCP relay
and over mediad's WebRTC route (which refuses ``robot.setMode`` by design), and a simulated robot
reports ``robot.policies.simulated``. Tools are gated on these answers, not on configuration.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from coincoin.robot.jsonrpc import API_VERSION, MIN_API_VERSION, JsonRpcError
from coincoin.robot.link import LinkDown, RobotLink, RobotRefused

LOGGER = logging.getLogger(__name__)
METHOD_NOT_FOUND = -32601
# A mode name no robotd accepts: robotd refuses it with `accepted: false` before touching any
# intent, while mediad's route refuses the method itself with METHOD_NOT_FOUND. Either answer is
# non-mutating, which is what makes this a safe probe on a real robot.
PROBE_MODE = "__coincoin_probe__"
REMOTE_REFUSAL = "not available over WebRTC"


def refused_by_route(error: BaseException) -> bool:
    """Whether a JSON-RPC error is a transport refusal rather than a robot answer."""
    return isinstance(error, JsonRpcError) and error.code == METHOD_NOT_FOUND


@dataclass(frozen=True)
class Capabilities:
    """The connected robot's identity and the actions this link may perform."""

    transport: str
    api_version: int | None = None
    daemon_version: str | None = None
    simulated: bool = False
    # None means the probe could not decide (link error); treat like False for safety.
    remote_mode_switch: bool | None = None
    mode: str | None = None
    policies_enabled: bool | None = None
    skills: tuple[str, ...] = ()
    producer: dict[str, str] = field(default_factory=dict)
    problems: tuple[str, ...] = ()

    @property
    def api_compatible(self) -> bool:
        return self.api_version is not None and self.api_version >= MIN_API_VERSION

    @property
    def mode_switch_needs_person(self) -> bool:
        return self.remote_mode_switch is not True

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["skills"] = list(self.skills)
        result["problems"] = list(self.problems)
        return result


def transport_of(link: RobotLink) -> str:
    name = type(link).__name__
    if name == "WebRtcLink":
        return "webrtc"
    if name == "TcpLink":
        return "tcp"
    return name


async def probe_mode_switch(link: RobotLink) -> bool | None:
    """Ask robot.setMode with an impossible mode: refused by route, or by robotd."""
    try:
        result = await link.call("robot.setMode", {"mode": PROBE_MODE})
    except JsonRpcError as error:
        if refused_by_route(error):
            return False
        LOGGER.info("setMode probe answered %s", error)
        return None
    except RobotRefused:
        return True
    except (LinkDown, TimeoutError):
        return None
    LOGGER.warning("robot accepted an impossible mode in the probe: %r", result)
    return True


async def probe(link: RobotLink) -> Capabilities:
    """Read identity, simulation flag, mode-switch reachability and skills from the robot."""
    problems: list[str] = []
    transport = transport_of(link)
    api_version: int | None = None
    daemon_version: str | None = None
    try:
        hello = await link.call("hello", {"api_version": API_VERSION})
        if isinstance(hello, dict):
            if type(hello.get("api_version")) is int:
                api_version = hello["api_version"]
            if isinstance(hello.get("daemon_version"), str):
                daemon_version = hello["daemon_version"]
    except (JsonRpcError, RobotRefused, LinkDown, TimeoutError) as error:
        problems.append(f"hello failed: {error}")
    simulated = False
    enabled: bool | None = None
    skills: tuple[str, ...] = ()
    try:
        policies = await link.call("robot.policies")
        if isinstance(policies, dict):
            simulated = policies.get("simulated") is True
            if type(policies.get("enabled")) is bool:
                enabled = policies["enabled"]
            if isinstance(policies.get("skills"), list):
                skills = tuple(name for name in policies["skills"] if isinstance(name, str))
    except (JsonRpcError, RobotRefused, LinkDown, TimeoutError) as error:
        problems.append(f"robot.policies failed: {error}")
    mode: str | None = None
    try:
        answer = await link.call("robot.mode")
        if isinstance(answer, dict) and answer.get("mode") in ("walk", "roller"):
            mode = answer["mode"]
    except (JsonRpcError, RobotRefused, LinkDown, TimeoutError) as error:
        problems.append(f"robot.mode failed: {error}")
    remote_mode_switch = await probe_mode_switch(link)
    producer = getattr(link, "producer_meta", None)
    return Capabilities(
        transport=transport,
        api_version=api_version,
        daemon_version=daemon_version,
        simulated=simulated,
        remote_mode_switch=remote_mode_switch,
        mode=mode,
        policies_enabled=enabled,
        skills=skills,
        producer=dict(producer) if isinstance(producer, dict) else {},
        problems=tuple(problems),
    )
