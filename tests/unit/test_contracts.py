import inspect
from dataclasses import FrozenInstanceError

import pytest

from coincoin.audio.io import FRAME_MS, SAMPLE_RATE, AudioIO
from coincoin.brain.base import Brain, NullBrain
from coincoin.robot.link import Frame, LinkDown, RobotLink, RobotRefused
from coincoin.robot.quack import QuackSink


def test_audio_frame_contract() -> None:
    assert SAMPLE_RATE == 24_000
    assert FRAME_MS == 20
    assert SAMPLE_RATE * FRAME_MS // 1000 * 2 == 960
    assert isinstance(inspect.getattr_static(AudioIO, "playing"), property)
    assert isinstance(inspect.getattr_static(AudioIO, "played_ms"), property)
    assert inspect.iscoroutinefunction(AudioIO.flush)


def test_transport_defaults_are_fixed() -> None:
    call = inspect.signature(RobotLink.call)
    assert list(call.parameters) == ["self", "method", "params", "timeout"]
    assert call.parameters["params"].default is None
    assert call.parameters["timeout"].default == 2.0
    assert inspect.signature(QuackSink.quack).parameters["hold_s"].default == 0.0
    assert inspect.iscoroutinefunction(QuackSink.cancel)
    assert isinstance(inspect.getattr_static(RobotLink, "connected"), property)


def test_frame_and_robot_errors() -> None:
    frame = Frame(b"jpeg", 640, 360, 123.5)
    assert frame.jpeg == b"jpeg"
    assert frame.width == 640 and frame.height == 360
    assert frame.captured_at == 123.5
    with pytest.raises(FrozenInstanceError):
        frame.__setattr__("width", 1)
    refused = RobotRefused("not homed")
    assert refused.reason == "not homed"
    assert str(refused) == "not homed"
    assert isinstance(LinkDown("offline"), Exception)


async def test_null_brain_is_explicitly_memory_free() -> None:
    brain = NullBrain()
    assert isinstance(brain, Brain)
    assert "pas de memoire" in await brain.session_start()
    assert "pas de memoire" in await brain.ask("remember me?")
    assert await brain.remember("a fact") is None
    assert await brain.session_end("a summary") is None
