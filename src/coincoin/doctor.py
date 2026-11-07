"""Connection doctor: walk the robot link step by step and say how to fix the first failure.

The same command runs against the simulator (``ROBOT_TARGET=sim``) and the delivered robot
(``ROBOT_TARGET=robot ROBOT_HOST=<ip>``), because both expose mediad's WebRTC path.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from coincoin.clock import SystemClock
from coincoin.config import Settings
from coincoin.robot.capabilities import Capabilities, probe
from coincoin.robot.factory import create_robot_link
from coincoin.robot.frames import FrameError
from coincoin.robot.jsonrpc import API_VERSION, JsonRpcError
from coincoin.robot.link import LinkDown, RobotLink, RobotRefused
from coincoin.robot.webrtc_link import signalling_url

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
ERRORS = (OSError, TimeoutError, LinkDown, RobotRefused, JsonRpcError, FrameError, ValueError)
LOW_BATTERY_PCT = 30
CRITICAL_BATTERY_PCT = 15
RTT_SAMPLES = 20

FIX_DISCOVER = (
    "find the duck's address with `duckctl ip` (BLE, works before Wi-Fi is known) or open its "
    "console with `duckctl open`; then set ROBOT_HOST=<ip>. The default hostname radxa-zero3 is "
    "not unique and mDNS is unreliable"
)
FIX_WIFI = (
    "put this computer on the duck's Wi-Fi; if the duck is not on Wi-Fi yet, provision it over "
    "Bluetooth with `duckctl wifi connect` (not possible over WebRTC)"
)
FIX_MEDIAD_SIM = (
    "start the simulator with a camera so duck-sim launches `mediad --sim-camera` "
    "(signalling on 8443, console on 8080), e.g. `scripts/live_stack.py --link webrtc`"
)
FIX_MEDIAD_ROBOT = (
    "on the duck, check the media service: `systemctl status mediad` / `journalctl -u mediad` "
    "(over ssh, or the console's services page via `duckctl open`)"
)
FIX_ICE = (
    "signalling works but the peer connection did not: WebRTC needs UDP between this computer "
    "and the duck on the same LAN (no guest-network client isolation, no VPN, allow UDP in the "
    "firewall)"
)


@dataclass
class Step:
    name: str
    status: str
    detail: str = ""
    fix: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"step": self.name, "status": self.status}
        if self.detail:
            result["detail"] = self.detail
        if self.fix:
            result["fix"] = self.fix
        if self.data:
            result["data"] = self.data
        return result


@dataclass
class Report:
    url: str
    target: str
    steps: list[Step] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(step.status != FAIL for step in self.steps)

    def add(self, step: Step) -> Step:
        self.steps.append(step)
        return step

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "url": self.url,
            "target": self.target or "(ROBOT_LINK)",
            "steps": [step.as_dict() for step in self.steps],
        }

    def text(self) -> str:
        marks = {OK: " OK ", WARN: "WARN", FAIL: "FAIL", SKIP: "SKIP"}
        lines = [f"coincoin doctor: {self.url} (target {self.target or 'ROBOT_LINK'})"]
        for step in self.steps:
            lines.append(f"[{marks[step.status]}] {step.name}: {step.detail}".rstrip(": "))
            if step.fix:
                lines.append(f"       fix: {step.fix}")
        lines.append("ready" if self.ok else "NOT ready: fix the first FAIL above and rerun")
        return "\n".join(lines)


async def _timed(work: Awaitable[Any]) -> tuple[Any, float]:
    started = time.perf_counter()
    result = await work
    return result, (time.perf_counter() - started) * 1000


async def _resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


async def _reach(host: str, port: int) -> float:
    async with asyncio.timeout(3):
        (_, writer), elapsed = await _timed(asyncio.open_connection(host, port))
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return elapsed


async def list_producers(url: str) -> list[dict[str, Any]]:
    """Ask mediad's signalling server which WebRTC producers (robots) it offers."""
    async with (
        asyncio.timeout(5),
        aiohttp.ClientSession() as session,
        session.ws_connect(signalling_url(url)) as ws,
    ):
        welcome = await ws.receive_json()
        if not isinstance(welcome, dict) or welcome.get("type") != "welcome":
            raise LinkDown("signalling server did not send welcome")
        await ws.send_json({"type": "list"})
        while True:
            message = await ws.receive_json()
            if isinstance(message, dict) and message.get("type") == "list":
                producers = message.get("producers")
                return [item for item in producers or [] if isinstance(item, dict)]


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


class Doctor:
    def __init__(
        self,
        settings: Settings,
        url: str | None = None,
        *,
        link_factory: Callable[[str], RobotLink] | None = None,
    ) -> None:
        self.settings = settings
        self.url = url or settings.robot_link
        self.target = settings.robot_target
        self._link_factory = link_factory
        parsed = urlsplit(self.url)
        self.scheme = parsed.scheme
        self.host = parsed.hostname or ""
        self.port = parsed.port or 0
        self.report = Report(self.url, self.target)

    @property
    def _fix_service(self) -> str:
        if self.scheme == "tcp":
            return "start the relay (`relay` / scripts/live_stack.py) or use ROBOT_TARGET=sim|robot"
        return FIX_MEDIAD_SIM if self.target != "robot" else FIX_MEDIAD_ROBOT

    def _link(self) -> RobotLink:
        if self._link_factory is not None:
            return self._link_factory(self.url)
        return create_robot_link(
            self.url,
            frame_url=self.settings.frame_link,
            token=self.settings.relay_token.get_secret_value(),
            clock=SystemClock(),
        )

    async def run(self) -> Report:
        add = self.report.add
        if self.scheme not in ("tcp", "webrtc") or not self.host or not self.port:
            add(Step("config", FAIL, f"unusable robot URL {self.url!r}", FIX_DISCOVER))
            return self.report
        add(
            Step(
                "config",
                OK if self.scheme == "webrtc" else WARN,
                f"{self.scheme} link to {self.host}:{self.port}",
                ""
                if self.scheme == "webrtc"
                else "TCP goes through the development relay; the delivered robot is reached "
                "with ROBOT_TARGET=robot ROBOT_HOST=<ip> (WebRTC)",
            )
        )
        try:
            addresses = await _resolve(self.host)
        except OSError as error:
            add(Step("resolve host", FAIL, f"{self.host}: {error}", FIX_DISCOVER))
            return self.report
        add(Step("resolve host", OK, ", ".join(addresses)))
        try:
            elapsed = await _reach(self.host, self.port)
        except (OSError, TimeoutError) as error:
            reason = str(error) or type(error).__name__
            fix = f"{self._fix_service}; if the duck is up, {FIX_WIFI}"
            add(Step("reach signalling", FAIL, f"{self.host}:{self.port}: {reason}", fix))
            return self.report
        add(Step("reach signalling", OK, f"TCP connect {elapsed:.0f} ms"))
        if self.scheme == "webrtc" and not await self._signalling():
            return self.report
        link = self._link()
        try:
            await self._connected(link)
        finally:
            await link.close()
        return self.report

    async def _signalling(self) -> bool:
        try:
            producers = await list_producers(self.url)
        except (*ERRORS, aiohttp.ClientError) as error:
            reason = str(error) or type(error).__name__
            self.report.add(Step("signalling", FAIL, reason, self._fix_service))
            return False
        if not producers:
            self.report.add(
                Step("signalling", FAIL, "mediad offers no robot producer", self._fix_service)
            )
            return False
        meta = producers[0].get("meta")
        meta = meta if isinstance(meta, dict) else {}
        described = ", ".join(f"{key}={value}" for key, value in sorted(meta.items()))
        self.report.add(Step("signalling", OK, described or "producer without metadata", data=meta))
        return True

    async def _connected(self, link: RobotLink) -> None:
        add = self.report.add
        name = "open WebRTC" if self.scheme == "webrtc" else "open link"
        try:
            async with asyncio.timeout(25):
                _, elapsed = await _timed(link.connect())
        except (*ERRORS, aiohttp.ClientError) as error:
            last = getattr(link, "last_error", "") or str(error)
            fix = FIX_ICE if self.scheme == "webrtc" else self._fix_service
            add(Step(name, FAIL, last or type(error).__name__, fix))
            return
        add(Step(name, OK, f"control channel up in {elapsed:.0f} ms"))
        capabilities = await probe(link)
        self._version(capabilities)
        await self._policies(link, capabilities)
        self._capabilities(capabilities)
        await self._health(link)
        await self._state(link)
        await self._latency(link)
        await self._camera(link)

    def _version(self, capabilities: Capabilities) -> None:
        version = capabilities.api_version
        daemon = capabilities.daemon_version or "?"
        if version is None:
            fix = "robotd did not answer hello; check `systemctl status robotd` on the duck"
            self.report.add(Step("api version", FAIL, "no hello answer", fix))
        elif version < API_VERSION:
            self.report.add(
                Step(
                    "api version",
                    FAIL,
                    f"robot API {version} ({daemon}) is older than Coin-Coin's {API_VERSION}",
                    "update the duck (console Updates page via `duckctl open`)",
                )
            )
        elif version > API_VERSION:
            self.report.add(
                Step(
                    "api version",
                    WARN,
                    f"robot API {version} ({daemon}) is newer than Coin-Coin's {API_VERSION}",
                    "robotd does not refuse a version mismatch; the methods Coin-Coin uses are "
                    "unchanged up to API 51, re-run the tests if the duck is newer",
                )
            )
        else:
            self.report.add(Step("api version", OK, f"API {version}, robotd {daemon}"))

    async def _policies(self, link: RobotLink, capabilities: Capabilities) -> None:
        try:
            policies = await link.call("robot.policies")
        except ERRORS as error:
            self.report.add(Step("policies", FAIL, str(error), "check robotd's journal"))
            return
        problems: list[str] = []
        if not isinstance(policies, dict):
            policies = {}
        for slot in policies.get("slots") or []:
            if isinstance(slot, dict) and slot.get("error"):
                problems.append(f"{slot.get('slot')}: {slot['error']}")
        if policies.get("change_error"):
            problems.append(f"last change: {policies['change_error']}")
        detail = (
            f"mode {policies.get('mode')}, enabled {policies.get('enabled')}, "
            f"{len(capabilities.skills)} skills; body protocol accepted by robotd at start-up"
        )
        if problems:
            self.report.add(
                Step(
                    "policies",
                    FAIL,
                    f"{detail}; " + "; ".join(problems),
                    "a policy slot failed to load: reinstall/select the policy from the console "
                    "(`duckctl open`) or `sudo robotctl configure` on the duck",
                )
            )
        else:
            self.report.add(Step("policies", OK, detail))

    def _capabilities(self, capabilities: Capabilities) -> None:
        add = self.report.add
        identity = "simulated" if capabilities.simulated else "real robot"
        if self.target == "sim" and not capabilities.simulated:
            add(
                Step(
                    "target",
                    FAIL,
                    "ROBOT_TARGET=sim but the robot does not report simulated:true",
                    "set ROBOT_TARGET=robot, or point ROBOT_HOST at the simulator",
                )
            )
        elif self.target == "robot" and capabilities.simulated:
            add(
                Step(
                    "target",
                    WARN,
                    "ROBOT_TARGET=robot but this robot is simulated",
                    "fine for rehearsal; sim-only tools stay enabled",
                )
            )
        else:
            add(Step("target", OK, f"{identity} over {capabilities.transport}"))
        switch = {
            True: "remote robot.setMode allowed",
            False: "robot.setMode refused by this link: a person fits skates and selects the mode",
            None: "robot.setMode reachability unknown: treated as refused",
        }[capabilities.remote_mode_switch]
        sim_only = "go_to and assisted get-up enabled" if capabilities.simulated else "sim-only "
        if not capabilities.simulated:
            sim_only += "tools (go_to, assisted get-up) disabled"
        add(
            Step(
                "capabilities",
                OK,
                f"{switch}; {sim_only}",
                data=capabilities.as_dict(),
            )
        )

    async def _health(self, link: RobotLink) -> None:
        try:
            health = await link.call("robot.health")
        except ERRORS as error:
            self.report.add(Step("health", FAIL, str(error), "check robotd's journal"))
            return
        if not isinstance(health, dict):
            self.report.add(Step("health", FAIL, "invalid robot.health answer"))
            return
        battery = health.get("battery") if isinstance(health.get("battery"), dict) else {}
        percent = battery.get("percent") if isinstance(battery, dict) else None
        volts = battery.get("volts") if isinstance(battery, dict) else None
        detail = f"healthy {health.get('healthy')}"
        if percent is not None:
            detail += f", battery {percent}%"
        if volts is not None:
            detail += f" ({volts} V)"
        if health.get("reason"):
            detail += f", {health['reason']}"
        status, fix = OK, ""
        if isinstance(percent, int | float) and percent < CRITICAL_BATTERY_PCT:
            status, fix = FAIL, "battery critical: charge the duck before moving it"
        elif isinstance(percent, int | float) and percent < LOW_BATTERY_PCT:
            status, fix = WARN, "battery low: charge soon"
        if health.get("healthy") is False:
            status = FAIL
            fix = fix or "robot unhealthy: read the reason; check motors and `journalctl -u robotd`"
        elif health.get("degraded"):
            status = WARN if status == OK else status
        self.report.add(Step("health", status, detail, fix, data=health))

    async def _state(self, link: RobotLink) -> None:
        for _ in range(40):
            if link.state() is not None:
                break
            await asyncio.sleep(0.05)
        age = link.state_age()
        if link.state() is None or age > 1.0:
            self.report.add(
                Step(
                    "state stream",
                    FAIL,
                    f"no fresh robot.state (age {age:.1f} s)",
                    "robot.subscribe is not delivering; restart mediad/robotd",
                )
            )
        else:
            self.report.add(Step("state stream", OK, f"latest state {age * 1000:.0f} ms old"))

    async def _latency(self, link: RobotLink) -> None:
        samples: list[float] = []
        try:
            for _ in range(RTT_SAMPLES):
                _, elapsed = await _timed(link.call("robot.mode"))
                samples.append(elapsed)
        except ERRORS as error:
            self.report.add(Step("command latency", FAIL, str(error)))
            return
        p50, p95 = statistics.median(samples), _percentile(samples, 0.95)
        status = OK if p95 < 150 else WARN
        fix = "" if status == OK else "slow Wi-Fi: move closer to the access point"
        data = {"p50_ms": round(p50, 1), "p95_ms": round(p95, 1), "samples": len(samples)}
        detail = f"robot.mode round trip p50 {p50:.1f} ms, p95 {p95:.1f} ms"
        self.report.add(Step("command latency", status, detail, fix, data=data))

    async def _camera(self, link: RobotLink) -> None:
        timings: list[float] = []
        frame = None
        try:
            for _ in range(3):
                frame, elapsed = await _timed(link.frame())
                timings.append(elapsed)
                await asyncio.sleep(0.1)
        except ERRORS as error:
            fix = (
                "no camera track: the robot needs a camera and mediad must publish it "
                "(simulator: duck-sim with camera)"
                if self.scheme == "webrtc"
                else "check FRAME_LINK and the relay's frame service"
            )
            self.report.add(Step("camera", FAIL, str(error) or type(error).__name__, fix))
            return
        assert frame is not None
        age = time.time() - frame.captured_at
        detail = (
            f"{frame.width}x{frame.height} JPEG {len(frame.jpeg) // 1024} KiB, first "
            f"{timings[0]:.0f} ms, then {statistics.median(timings[1:]):.0f} ms, age {age:.2f} s"
        )
        data = {"first_ms": round(timings[0], 1), "next_ms": round(timings[-1], 1)}
        self.report.add(Step("camera", OK if age < 2 else WARN, detail, data=data))


async def run_doctor(settings: Settings, url: str | None = None) -> Report:
    return await Doctor(settings, url).run()
