"""Typed normalization for the Realtime server events used by Coin-Coin."""

import base64
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Event(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AudioDelta(_Event):
    item_id: str
    pcm: bytes
    content_index: int = 0


class AssistantTranscript(_Event):
    item_id: str
    text: str
    final: bool = False


class UserTranscript(_Event):
    item_id: str
    text: str
    final: bool = False


class SpeechStarted(_Event):
    item_id: str | None = None
    audio_start_ms: int | None = None


class SpeechStopped(_Event):
    item_id: str | None = None
    audio_end_ms: int | None = None


class ResponseStarted(_Event):
    response_id: str | None = None


class ToolCall(_Event):
    call_id: str
    name: str
    arguments: str
    item_id: str | None = None


class ResponseDone(_Event):
    status: str
    usage: dict[str, Any] = Field(default_factory=dict)
    response_id: str | None = None


class Error(_Event):
    error_type: str
    message: str
    code: str | None = None
    param: str | None = None


type RealtimeEvent = (
    AudioDelta
    | AssistantTranscript
    | UserTranscript
    | SpeechStarted
    | SpeechStopped
    | ResponseStarted
    | ToolCall
    | ResponseDone
    | Error
)


def parse_event(payload: dict[str, Any]) -> RealtimeEvent | None:
    """Normalize documented GA server events; ignore events the app does not consume."""
    kind = payload.get("type")
    if kind == "response.output_audio.delta":
        return AudioDelta(
            item_id=payload["item_id"],
            pcm=base64.b64decode(payload["delta"], validate=True),
            content_index=payload.get("content_index", 0),
        )
    if kind in {"response.output_audio_transcript.delta", "response.output_audio_transcript.done"}:
        return AssistantTranscript(
            item_id=payload["item_id"],
            text=payload["delta"] if kind.endswith(".delta") else payload["transcript"],
            final=kind.endswith(".done"),
        )
    if kind in {
        "conversation.item.input_audio_transcription.delta",
        "conversation.item.input_audio_transcription.completed",
    }:
        return UserTranscript(
            item_id=payload["item_id"],
            text=payload["delta"] if kind.endswith(".delta") else payload["transcript"],
            final=kind.endswith(".completed"),
        )
    if kind == "input_audio_buffer.speech_started":
        return SpeechStarted(
            item_id=payload.get("item_id"), audio_start_ms=payload.get("audio_start_ms")
        )
    if kind == "input_audio_buffer.speech_stopped":
        return SpeechStopped(
            item_id=payload.get("item_id"), audio_end_ms=payload.get("audio_end_ms")
        )
    if kind == "response.created":
        response = payload.get("response")
        return ResponseStarted(
            response_id=response.get("id") if isinstance(response, dict) else None
        )
    if kind == "response.output_item.done":
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            return ToolCall(
                call_id=item["call_id"],
                name=item["name"],
                arguments=item.get("arguments", "{}"),
                item_id=item.get("id"),
            )
    if kind == "response.done":
        response = payload.get("response")
        if isinstance(response, dict):
            usage = response.get("usage", {})
            return ResponseDone(
                status=response.get("status", "unknown"),
                usage=usage if isinstance(usage, dict) else {},
                response_id=response.get("id"),
            )
    if kind == "error":
        error = payload.get("error")
        if isinstance(error, dict):
            return Error(
                error_type=error.get("type", "unknown"),
                message=error.get("message", "Realtime API error"),
                code=error.get("code"),
                param=error.get("param"),
            )
        return Error(error_type="unknown", message="Realtime API error")
    return None
