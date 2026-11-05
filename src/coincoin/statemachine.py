"""Pure conversation-state transitions and declarative side-effect descriptions."""

import math
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal


class Mode(StrEnum):
    SLEEPING = "SLEEPING"
    AWAKE = "AWAKE"
    FOLLOWUP = "FOLLOWUP"


@dataclass(frozen=True)
class State:
    mode: Mode = Mode.SLEEPING
    speaking: bool = False
    wake_enabled: bool = True
    wake_enable_at: float | None = None
    followup_deadline: float | None = None
    followup_s: float = 420
    response_active: bool = False
    quack_active: bool = False


EventKind = Literal[
    "wake",
    "session_ready",
    "user_speech_start",
    "user_speech_end",
    "response_started",
    "response_done",
    "response_failed",
    "quack_started",
    "quack_done",
    "sleep_requested",
    "stop_pressed",
    "tick",
]


@dataclass(frozen=True)
class Event:
    kind: EventKind
    question: bool = False
    summary: str = ""


@dataclass(frozen=True)
class Effect:
    kind: Literal[
        "OpenSession",
        "CloseSession",
        "Quack",
        "RobotStop",
        "ResetPose",
        "MouthClose",
        "FlushPlayback",
        "CancelResponse",
        "BrainSessionStart",
        "BrainSessionEnd",
        "SetWakeEnabled",
        "SetGate",
    ]
    value: str | bool | None = None


def _sleep(state: State, *, stopped: bool = False, summary: str = "") -> tuple[State, list[Effect]]:
    effects = [
        Effect("RobotStop"),
        Effect("ResetPose"),
        Effect("MouthClose"),
        Effect("SetWakeEnabled", False),
        Effect("SetGate", Mode.SLEEPING.value),
        Effect("BrainSessionEnd", summary),
        Effect("CloseSession"),
    ]
    if not stopped:
        effects.insert(5, Effect("Quack", "coo"))
    if stopped or state.speaking:
        effects[0:0] = [Effect("FlushPlayback"), Effect("CancelResponse")]
    return State(
        mode=Mode.SLEEPING,
        speaking=not stopped,
        quack_active=not stopped,
        wake_enabled=False,
        followup_s=state.followup_s,
    ), effects


def transition(state: State, event: Event, now: float) -> tuple[State, list[Effect]]:
    """Apply one event without I/O; returned effects are executed by the caller."""
    if not math.isfinite(now):
        raise ValueError("Transition time must be finite")
    if event.kind == "wake":
        if state.mode != Mode.SLEEPING or not state.wake_enabled:
            return state, []
        return (
            State(
                mode=Mode.AWAKE,
                wake_enabled=False,
                wake_enable_at=now + 0.5,
                followup_s=state.followup_s,
            ),
            [
                Effect("Quack", "greet"),
                Effect("OpenSession"),
                Effect("BrainSessionStart"),
                Effect("SetGate", "AWAKE_BUFFER"),
                Effect("SetWakeEnabled", False),
            ],
        )
    if event.kind == "session_ready":
        if state.mode == Mode.SLEEPING:
            return state, []
        return replace(state), [Effect("SetGate", state.mode.value)]
    if event.kind == "quack_started" and state.mode == Mode.SLEEPING:
        return (
            replace(
                state, speaking=True, quack_active=True, wake_enabled=False, wake_enable_at=None
            ),
            [Effect("SetWakeEnabled", False)],
        )
    if event.kind in ("response_started", "quack_started"):
        if state.mode == Mode.SLEEPING:
            return state, []
        return (
            replace(
                state,
                speaking=True,
                response_active=state.response_active or event.kind == "response_started",
                quack_active=state.quack_active or event.kind == "quack_started",
                wake_enabled=False,
                wake_enable_at=None,
            ),
            [Effect("SetWakeEnabled", False)],
        )
    if event.kind == "quack_done" and state.mode == Mode.SLEEPING:
        if not state.speaking:
            return state, []
        return (
            replace(
                state,
                speaking=False,
                quack_active=False,
                wake_enabled=False,
                wake_enable_at=now + 0.5,
            ),
            [Effect("MouthClose"), Effect("SetWakeEnabled", False)],
        )
    if event.kind in ("response_done", "quack_done"):
        if state.mode == Mode.SLEEPING:
            return state, []
        mode = Mode.FOLLOWUP if event.kind == "response_done" else state.mode
        response_active = state.response_active and event.kind != "response_done"
        quack_active = state.quack_active and event.kind != "quack_done"
        inquire = event.kind == "response_done" and event.question
        speaking = response_active or quack_active or inquire
        effects = [] if response_active or quack_active else [Effect("MouthClose")]
        if inquire:
            effects.append(Effect("Quack", "inquire"))
        effects.extend(
            [
                Effect("SetWakeEnabled", False),
                Effect("SetGate", mode.value),
            ]
        )
        return (
            replace(
                state,
                mode=mode,
                speaking=speaking,
                response_active=response_active,
                quack_active=quack_active or inquire,
                wake_enabled=False,
                wake_enable_at=None if speaking else now + 0.5,
                followup_deadline=now + state.followup_s
                if event.kind == "response_done"
                else state.followup_deadline,
            ),
            effects,
        )
    if event.kind == "response_failed":
        if state.mode == Mode.SLEEPING:
            return state, []
        speaking = state.quack_active
        effects = [] if speaking else [Effect("MouthClose")]
        effects.append(Effect("SetWakeEnabled", False))
        return (
            replace(
                state,
                speaking=speaking,
                response_active=False,
                wake_enabled=False,
                wake_enable_at=None if speaking else now + 0.5,
            ),
            effects,
        )
    if event.kind == "user_speech_start":
        if state.speaking:
            return (
                replace(
                    state,
                    speaking=False,
                    response_active=False,
                    quack_active=False,
                    wake_enabled=False,
                    wake_enable_at=now + 0.5,
                    followup_deadline=now + state.followup_s
                    if state.mode == Mode.FOLLOWUP
                    else state.followup_deadline,
                ),
                [
                    Effect("FlushPlayback"),
                    Effect("CancelResponse"),
                    Effect("MouthClose"),
                    Effect("SetWakeEnabled", False),
                ],
            )
        if state.mode == Mode.FOLLOWUP:
            return replace(state, followup_deadline=now + state.followup_s), [
                Effect("SetGate", Mode.FOLLOWUP.value)
            ]
        return state, []
    if event.kind == "user_speech_end":
        return state, []
    if event.kind == "sleep_requested":
        if state.mode == Mode.SLEEPING:
            return state, []
        return _sleep(state, summary=event.summary)
    if event.kind == "stop_pressed":
        return _sleep(state, stopped=True, summary=event.summary)
    if event.kind == "tick":
        if (
            state.mode == Mode.FOLLOWUP
            and state.followup_deadline is not None
            and now >= state.followup_deadline
        ):
            return _sleep(state)
        if (
            not state.wake_enabled
            and not state.speaking
            and state.wake_enable_at is not None
            and now >= state.wake_enable_at
        ):
            return replace(state, wake_enabled=True, wake_enable_at=None), [
                Effect("SetWakeEnabled", True)
            ]
        return state, []
    return state, []
