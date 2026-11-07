"""JSONL session recordings of a robot link, and a link that replays them offline.

A recording is one JSON object per line, each with ``t`` (seconds since the recording started,
monotonic) and ``kind``:

``header``  first line: format version, start time, link scheme, API version and the robot's
            ``hello``/``robot.policies``/``robot.health``/``robot.mode`` answers when available.
``state``   one pushed ``robot.state`` (only when it changed since the previous sample).
``tof``     one ``tof.frame`` (8x8 ``distance_mm``/``status``), when a ToF stream was recorded.
``frame``   one camera snapshot: base64 JPEG, width, height, ``captured_at`` (Unix seconds).
``call``    a request coin-coin (or the recorder) sent: method, params, then result or error.
``notify``  a notification coin-coin (or the recorder) sent: method, params.
``note``    free text, e.g. "lifted", "walk 1 m", written by the operator.

The same file therefore holds both what the robot reported and what was asked of it, so a replay
can feed the reports back and compare the commands a new build sends against the recorded ones.
"""

from __future__ import annotations

import base64
import copy
import json
import math
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from coincoin.clock import Clock, SystemClock
from coincoin.robot.frames import FrameError
from coincoin.robot.link import Frame, LinkDown, RobotLink, RobotRefused

FORMAT = "coincoin-session/1"
KINDS = frozenset({"header", "state", "tof", "frame", "call", "notify", "note"})


@dataclass(frozen=True)
class Event:
    """One recorded line."""

    t: float
    kind: str
    data: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps({"t": round(self.t, 6), "kind": self.kind, **self.data}, default=str)

    @classmethod
    def from_json(cls, line: str) -> Event:
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError("Recording line is not a JSON object")
        t = raw.pop("t", None)
        kind = raw.pop("kind", None)
        if not isinstance(t, int | float) or not math.isfinite(t) or t < 0:
            raise ValueError("Recording line has no valid t")
        if kind not in KINDS:
            raise ValueError(f"Unknown recording kind: {kind!r}")
        return cls(float(t), str(kind), raw)


def read_events(path: Path | str) -> list[Event]:
    """Load a recording, rejecting files that are not this format or are out of order."""
    events: list[Event] = []
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = Event.from_json(line)
            except ValueError as error:
                raise ValueError(f"{path}:{number}: {error}") from error
            if events and event.t < events[-1].t:
                raise ValueError(f"{path}:{number}: time goes backwards")
            events.append(event)
    if not events or events[0].kind != "header" or events[0].data.get("format") != FORMAT:
        raise ValueError(f"{path}: not a {FORMAT} recording")
    return events


class SessionWriter:
    """Append events to a JSONL file, flushing each line so a crash keeps what was recorded."""

    def __init__(self, handle: IO[str], clock: Clock | None = None) -> None:
        self.handle = handle
        self.clock = clock if clock is not None else SystemClock()
        self.started = self.clock.now()
        self.counts: dict[str, int] = {}

    def elapsed(self) -> float:
        return max(0.0, self.clock.now() - self.started)

    def write(self, kind: str, **data: Any) -> None:
        if kind not in KINDS:
            raise ValueError(f"Unknown recording kind: {kind!r}")
        self.handle.write(Event(self.elapsed(), kind, data).to_json() + "\n")
        self.handle.flush()
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def header(self, **data: Any) -> None:
        self.write("header", format=FORMAT, started_unix=time.time(), **data)

    def frame(self, frame: Frame) -> None:
        self.write(
            "frame",
            jpeg_b64=base64.b64encode(frame.jpeg).decode("ascii"),
            width=frame.width,
            height=frame.height,
            captured_at=frame.captured_at,
        )


class RecordingLink:
    """A RobotLink that forwards to another link and records every command and its outcome."""

    def __init__(self, inner: RobotLink, writer: SessionWriter) -> None:
        self.inner = inner
        self.writer = writer

    async def connect(self) -> None:
        await self.inner.connect()

    async def close(self) -> None:
        await self.inner.close()

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 2.0,  # noqa: ASYNC109 - RobotLink contract
    ) -> Any:
        started = self.writer.elapsed()
        try:
            result = await self.inner.call(method, params, timeout)
        except RobotRefused as error:
            self.writer.write(
                "call", method=method, params=params, refused=error.reason, sent=started
            )
            raise
        except Exception as error:
            self.writer.write(
                "call",
                method=method,
                params=params,
                error=f"{type(error).__name__}: {error}",
                sent=started,
            )
            raise
        self.writer.write("call", method=method, params=params, result=result, sent=started)
        return result

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.writer.write("notify", method=method, params=params)
        await self.inner.notify(method, params)

    def state(self) -> dict[str, Any] | None:
        return self.inner.state()

    def state_age(self) -> float:
        return self.inner.state_age()

    async def frame(self) -> Frame:
        frame = await self.inner.frame()
        self.writer.frame(frame)
        return frame

    @property
    def connected(self) -> bool:
        return self.inner.connected


def _same_state(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    if a is None or b is None:
        return a is b
    if "t" in a and "t" in b:
        return a["t"] == b["t"]
    return a == b


class StateSampler:
    """Record a link's cached state whenever it changes; call ``sample`` at least as fast as
    the subscription rate (10 Hz for coin-coin's ``robot.subscribe``)."""

    def __init__(self, link: RobotLink, writer: SessionWriter) -> None:
        self.link = link
        self.writer = writer
        self.last: dict[str, Any] | None = None

    def sample(self) -> bool:
        state = self.link.state()
        if state is None or _same_state(state, self.last):
            return False
        self.last = copy.deepcopy(state)
        self.writer.write("state", state=state, age=_finite_or_none(self.link.state_age()))
        return True


def _finite_or_none(value: float) -> float | None:
    return value if math.isfinite(value) else None


@dataclass
class ReplayedCommand:
    """A command coin-coin sent to the replay link, at replay time ``t``."""

    t: float
    method: str
    params: dict[str, Any] | None
    notification: bool


# Answers for calls the recording does not contain, so a replayed session can start the same way
# a live one does. Everything else is refused, which is what a robot does with a method it lacks.
DEFAULT_RESPONSES: dict[str, Any] = {
    "hello": {"api_version": None},
    "robot.subscribe": {"accepted": True},
    "robot.stop": None,
    "robot.enable": None,
}


@dataclass
class _Timeline:
    times: list[float] = field(default_factory=list)
    items: list[Any] = field(default_factory=list)

    def add(self, t: float, item: Any) -> None:
        self.times.append(t)
        self.items.append(item)

    def latest(self, now: float) -> tuple[float, Any] | None:
        low, high = 0, len(self.times)
        while low < high:
            middle = (low + high) // 2
            if self.times[middle] <= now:
                low = middle + 1
            else:
                high = middle
        if low == 0:
            return None
        return self.times[low - 1], self.items[low - 1]


class ReplayLink:
    """A RobotLink that plays a recording back on a clock, for offline tests.

    ``state()``, ``state_age()`` and ``frame()`` answer from the recording at the current replay
    time (``speed`` times the clock since ``connect``). After the last state, ``state_age`` keeps
    growing, so a governor sees a stale robot rather than a frozen one. ``call`` answers with the
    next recorded result for that method (then repeats the last one), re-raises recorded refusals,
    and refuses methods that were never recorded and have no default. Everything sent is kept in
    ``commands`` for comparison with the recording's own ``call``/``notify`` lines.
    """

    def __init__(
        self,
        events: Iterable[Event] | Path | str,
        *,
        clock: Clock | None = None,
        speed: float = 1.0,
        loop: bool = False,
    ) -> None:
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError("speed must be finite and positive")
        self.events = read_events(events) if isinstance(events, Path | str) else list(events)
        if not self.events or self.events[0].kind != "header":
            raise ValueError("A replay needs a recording that starts with its header")
        self.header = self.events[0].data
        self.clock = clock if clock is not None else SystemClock()
        self.speed = speed
        self.loop = loop
        self.duration = self.events[-1].t
        self.commands: list[ReplayedCommand] = []
        self._states = _Timeline()
        self._frames = _Timeline()
        self._tof = _Timeline()
        self._answers: dict[str, list[dict[str, Any]]] = {}
        self._answered: dict[str, int] = {}
        for method, value in (self.header.get("probes") or {}).items():
            if not (isinstance(value, dict) and set(value) == {"error"}):
                self._answers.setdefault(method, []).append({"method": method, "result": value})
        for event in self.events:
            if event.kind == "state":
                self._states.add(event.t, event.data.get("state"))
            elif event.kind == "frame":
                self._frames.add(event.t, event.data)
            elif event.kind == "tof":
                self._tof.add(event.t, event.data.get("frame"))
            elif event.kind == "call":
                self._answers.setdefault(str(event.data.get("method")), []).append(event.data)
        self._started: float | None = None
        self._connected = False

    def elapsed(self) -> float:
        if self._started is None:
            return 0.0
        elapsed = (self.clock.now() - self._started) * self.speed
        if self.loop and self.duration > 0:
            return elapsed % self.duration
        return elapsed

    @property
    def finished(self) -> bool:
        return not self.loop and self._started is not None and self.elapsed() > self.duration

    async def connect(self) -> None:
        self._started = self.clock.now()
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    def _check(self) -> None:
        if not self._connected:
            raise LinkDown("replay link is closed")

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 2.0,  # noqa: ASYNC109 - RobotLink contract
    ) -> Any:
        self._check()
        self.commands.append(ReplayedCommand(self.elapsed(), method, copy.deepcopy(params), False))
        answers = self._answers.get(method)
        if not answers:
            if method in DEFAULT_RESPONSES:
                response = copy.deepcopy(DEFAULT_RESPONSES[method])
                if method == "hello":
                    response["api_version"] = self.header.get("api_version")
                return response
            raise RobotRefused(f"{method} was not recorded")
        index = self._answered.get(method, 0)
        self._answered[method] = index + 1
        answer = answers[min(index, len(answers) - 1)]
        if "refused" in answer:
            raise RobotRefused(str(answer["refused"]))
        if "error" in answer:
            raise LinkDown(str(answer["error"]))
        return copy.deepcopy(answer.get("result"))

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._check()
        self.commands.append(ReplayedCommand(self.elapsed(), method, copy.deepcopy(params), True))

    def state(self) -> dict[str, Any] | None:
        latest = self._states.latest(self.elapsed())
        return None if latest is None else copy.deepcopy(latest[1])

    def state_age(self) -> float:
        latest = self._states.latest(self.elapsed())
        if latest is None or self._started is None:
            return math.inf
        if self.loop:
            return max(0.0, self.elapsed() - latest[0])
        # Past the end the recording has nothing newer: the robot went quiet.
        return max(0.0, (self.clock.now() - self._started) * self.speed - latest[0])

    def tof(self) -> dict[str, Any] | None:
        """The latest recorded ``tof.frame`` at replay time, or None."""
        latest = self._tof.latest(self.elapsed())
        return None if latest is None else copy.deepcopy(latest[1])

    async def frame(self) -> Frame:
        self._check()
        latest = self._frames.latest(self.elapsed())
        if latest is None:
            raise FrameError("No recorded camera frame yet")
        data = latest[1]
        return Frame(
            jpeg=base64.b64decode(data["jpeg_b64"]),
            width=int(data["width"]),
            height=int(data["height"]),
            captured_at=float(data["captured_at"]),
        )

    @property
    def connected(self) -> bool:
        return self._connected


def recorded_commands(events: Iterable[Event]) -> Iterator[tuple[float, str, bool]]:
    """(t, method, notification) for each command in a recording."""
    for event in events:
        if event.kind in ("call", "notify"):
            yield event.t, str(event.data.get("method")), event.kind == "notify"


def summarize(events: list[Event]) -> dict[str, Any]:
    """Counts, rates and the safety/move facts that differ most between sim and hardware."""
    counts: dict[str, int] = {}
    for event in events:
        counts[event.kind] = counts.get(event.kind, 0) + 1
    states = [event for event in events if event.kind == "state"]
    duration = events[-1].t if events else 0.0
    gaps = [b.t - a.t for a, b in zip(states, states[1:], strict=False)]
    flags: dict[str, int] = {}
    loop_hz: list[float] = []
    for event in states:
        state = event.data.get("state") or {}
        for key, value in (state.get("safety") or {}).items():
            if value is True:
                flags[key] = flags.get(key, 0) + 1
        hz = (state.get("loop") or {}).get("hz")
        if isinstance(hz, int | float):
            loop_hz.append(float(hz))
    methods: dict[str, int] = {}
    for _, method, _ in recorded_commands(events):
        methods[method] = methods.get(method, 0) + 1
    header = events[0].data if events else {}
    return {
        "format": header.get("format"),
        "link": header.get("link"),
        "api_version": header.get("api_version"),
        "duration_s": round(duration, 3),
        "counts": counts,
        "state_hz": round(len(states) / duration, 2) if duration > 0 else None,
        "state_gap_max_s": round(max(gaps), 3) if gaps else None,
        "loop_hz_min": min(loop_hz) if loop_hz else None,
        "safety_true_samples": flags,
        "commands": methods,
    }
