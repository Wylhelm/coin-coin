import json
from pathlib import Path

from coincoin.realtime.events import (
    AssistantTranscript,
    AudioDelta,
    ResponseDone,
    ResponseStarted,
    SpeechStarted,
    SpeechStopped,
    ToolCall,
    UserTranscript,
    parse_event,
)


def _trace_events() -> list[dict[str, object]]:
    path = Path(__file__).parents[1] / "fakes" / "fixtures" / "realtime_trace.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_real_trace_event_names_and_payloads_are_normalized() -> None:
    events = [parse_event(event) for event in _trace_events()]
    normalized = [event for event in events if event is not None]

    assert any(isinstance(event, AudioDelta) and event.pcm for event in normalized)
    assert any(isinstance(event, AssistantTranscript) for event in normalized)
    assert any(isinstance(event, UserTranscript) and event.final for event in normalized)
    assert any(isinstance(event, ResponseStarted) for event in normalized)
    assert any(isinstance(event, ToolCall) and event.name == "ping" for event in normalized)
    assert any(
        isinstance(event, ResponseDone) and event.status == "completed" and event.usage
        for event in normalized
    )
    assert any(
        isinstance(event, ResponseDone) and event.status == "cancelled" for event in normalized
    )


def test_server_vad_events_use_ga_event_names() -> None:
    started = parse_event(
        {"type": "input_audio_buffer.speech_started", "item_id": "item-1", "audio_start_ms": 40}
    )
    stopped = parse_event(
        {"type": "input_audio_buffer.speech_stopped", "item_id": "item-1", "audio_end_ms": 300}
    )
    assert isinstance(started, SpeechStarted) and started.audio_start_ms == 40
    assert isinstance(stopped, SpeechStopped) and stopped.audio_end_ms == 300


def test_unconsumed_events_are_ignored() -> None:
    assert parse_event({"type": "rate_limits.updated", "rate_limits": []}) is None
