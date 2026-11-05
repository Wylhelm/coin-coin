import pytest

from coincoin.clock import FakeClock
from coincoin.statemachine import Effect, Event, EventKind, Mode, State, transition


def kinds(effects: list[Effect]) -> list[tuple[str, str | bool | None]]:
    return [(effect.kind, effect.value) for effect in effects]


@pytest.mark.parametrize(
    "kind",
    [
        "wake",
        "session_ready",
        "user_speech_start",
        "user_speech_end",
        "response_started",
        "response_done",
        "quack_started",
        "quack_done",
        "sleep_requested",
        "stop_pressed",
        "tick",
    ],
)
def test_every_event_returns_a_state_and_effect_list(kind: EventKind) -> None:
    state, effects = transition(State(), Event(kind), 0)
    assert isinstance(state, State)
    assert isinstance(effects, list)


def test_wake_opens_session_and_greets_once() -> None:
    state, effects = transition(State(), Event("wake"), 10)
    assert state == State(Mode.AWAKE, wake_enabled=False, wake_enable_at=10.5)
    assert kinds(effects) == [
        ("Quack", "greet"),
        ("OpenSession", None),
        ("BrainSessionStart", None),
        ("SetGate", "AWAKE_BUFFER"),
        ("SetWakeEnabled", False),
    ]
    assert transition(state, Event("wake"), 11) == (state, [])


def test_wake_is_ignored_outside_sleep_even_while_talking() -> None:
    state = State(Mode.AWAKE, speaking=True, wake_enabled=False)
    assert transition(state, Event("wake"), 0) == (state, [])


def test_session_ready_does_nothing_while_sleeping() -> None:
    assert transition(State(), Event("session_ready"), 0) == (State(), [])


def test_response_lifecycle_enters_followup_and_closes_mouth() -> None:
    awake = State(Mode.AWAKE, speaking=True, wake_enabled=False)
    state, effects = transition(awake, Event("response_done", question=True), 5)
    assert state.mode == Mode.FOLLOWUP
    assert state.followup_deadline == 425
    assert state.speaking
    assert state.quack_active
    assert not state.response_active
    assert ("MouthClose", None) in kinds(effects)
    assert ("Quack", "inquire") in kinds(effects)
    assert ("SetGate", Mode.FOLLOWUP.value) in kinds(effects)


def test_followup_deadline_rearms_on_user_speech() -> None:
    followup = State(Mode.FOLLOWUP, followup_deadline=420)
    active, effects = transition(followup, Event("user_speech_start"), 300)
    assert active.followup_deadline == 720
    assert kinds(effects) == [("SetGate", Mode.FOLLOWUP.value)]


def test_followup_duration_is_state_configurable() -> None:
    awake = State(Mode.AWAKE, speaking=True, followup_s=60)
    followup, _ = transition(awake, Event("response_done"), 100)
    assert followup.followup_deadline == 160
    asleep, _ = transition(followup, Event("tick"), 160)
    assert asleep.followup_s == 60


def test_followup_sleeps_only_after_deadline() -> None:
    clock = FakeClock()
    state, _ = transition(State(Mode.AWAKE, speaking=True), Event("response_done"), clock.now())
    assert state.followup_deadline == 420
    clock.advance(419)
    before, effects = transition(state, Event("tick"), clock.now())
    assert before.mode == Mode.FOLLOWUP
    assert before.followup_deadline == 420
    assert before.wake_enabled
    assert effects == [Effect("SetWakeEnabled", True)]
    clock.advance(2)
    after, effects = transition(before, Event("tick"), clock.now())
    assert after.mode == Mode.SLEEPING
    assert after.speaking
    assert not after.wake_enabled
    assert [effect.kind for effect in effects] == [
        "RobotStop",
        "ResetPose",
        "MouthClose",
        "SetWakeEnabled",
        "SetGate",
        "Quack",
        "BrainSessionEnd",
        "CloseSession",
    ]


def test_barge_in_flushes_playback_and_cancels_response() -> None:
    state = State(Mode.AWAKE, speaking=True, wake_enabled=False)
    next_state, effects = transition(state, Event("user_speech_start"), 2)
    assert next_state.mode == Mode.AWAKE
    assert not next_state.speaking
    assert kinds(effects) == [
        ("FlushPlayback", None),
        ("CancelResponse", None),
        ("MouthClose", None),
        ("SetWakeEnabled", False),
    ]


def test_wake_reenables_half_second_after_speech_or_quack() -> None:
    awake, _ = transition(State(), Event("wake"), 0)
    speaking, _ = transition(awake, Event("quack_started"), 1)
    finished, effects = transition(speaking, Event("quack_done"), 2)
    assert not finished.wake_enabled
    assert finished.wake_enable_at == 2.5
    still_disabled, effects = transition(finished, Event("tick"), 2.49)
    assert not still_disabled.wake_enabled
    assert effects == []
    enabled, effects = transition(finished, Event("tick"), 2.5)
    assert enabled.wake_enabled
    assert enabled.wake_enable_at is None
    assert kinds(effects) == [("SetWakeEnabled", True)]


def test_sleep_and_stop_emit_distinct_cleanup_effects() -> None:
    active = State(Mode.FOLLOWUP, speaking=True, followup_deadline=99)
    asleep, effects = transition(active, Event("sleep_requested", summary="bye"), 1)
    assert asleep.mode == Mode.SLEEPING
    assert asleep.speaking
    assert not asleep.wake_enabled
    assert ("BrainSessionEnd", "bye") in kinds(effects)
    stopped, effects = transition(active, Event("stop_pressed"), 1)
    assert stopped.mode == Mode.SLEEPING
    assert stopped.speaking
    assert kinds(effects)[:2] == [("FlushPlayback", None), ("CancelResponse", None)]


def test_sleep_quack_keeps_wake_disabled_until_500ms_after_it_finishes() -> None:
    sleeping, _ = transition(State(Mode.FOLLOWUP), Event("sleep_requested"), 0)
    assert not sleeping.wake_enabled
    assert transition(sleeping, Event("wake"), 1) == (sleeping, [])
    quiet, _ = transition(sleeping, Event("quack_done"), 2)
    assert not quiet.speaking
    assert quiet.wake_enable_at == 2.5
    still_asleep, _ = transition(quiet, Event("tick"), 2.49)
    assert not still_asleep.wake_enabled
    awake, _ = transition(quiet, Event("tick"), 2.5)
    assert awake.wake_enabled
    woken, _ = transition(awake, Event("wake"), 3)
    assert woken.mode == Mode.AWAKE


def test_non_finite_transition_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        transition(State(), Event("tick"), float("nan"))


@pytest.mark.parametrize("first_done", ["quack_done", "response_done"])
def test_overlapping_response_and_quack_preserve_remaining_playback(
    first_done: EventKind,
) -> None:
    state, _ = transition(State(), Event("wake"), 0)
    state, _ = transition(state, Event("quack_started"), 0.1)
    state, _ = transition(state, Event("response_started"), 0.2)
    remaining, effects = transition(state, Event(first_done), 0.3)
    assert remaining.speaking
    assert remaining.response_active == (first_done == "quack_done")
    assert remaining.quack_active == (first_done == "response_done")
    assert remaining.wake_enable_at is None
    assert ("MouthClose", None) not in kinds(effects)
    assert not transition(remaining, Event("tick"), 1)[0].wake_enabled
    interrupted, effects = transition(remaining, Event("user_speech_start"), 0.4)
    assert not interrupted.speaking
    assert not interrupted.response_active
    assert not interrupted.quack_active
    assert ("FlushPlayback", None) in kinds(effects)
    assert ("CancelResponse", None) in kinds(effects)
    second_done: EventKind = "response_done" if first_done == "quack_done" else "quack_done"
    finished, effects = transition(remaining, Event(second_done), 2)
    assert not finished.speaking
    assert not finished.response_active
    assert not finished.quack_active
    assert finished.wake_enable_at == 2.5
    assert ("MouthClose", None) in kinds(effects)
    assert not transition(finished, Event("tick"), 2.49)[0].wake_enabled
    assert transition(finished, Event("tick"), 2.5)[0].wake_enabled
    assert finished.followup_deadline == (422 if first_done == "quack_done" else 420.3)


def test_inquiry_quack_keeps_wake_disabled_until_playback_finishes() -> None:
    state, _ = transition(State(Mode.AWAKE), Event("response_started"), 0)
    inquiry, _ = transition(state, Event("response_done", question=True), 1)
    assert inquiry.speaking
    assert inquiry.quack_active
    assert not transition(inquiry, Event("tick"), 2)[0].wake_enabled
    quiet, _ = transition(inquiry, Event("quack_done"), 3)
    assert quiet.followup_deadline == inquiry.followup_deadline
    assert quiet.wake_enable_at == 3.5
