"""Record one short, sanitized trace from the GA Realtime WebSocket API."""

import argparse
import asyncio
import base64
import io
import json
import math
import re
import wave
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

from coincoin.config import Settings

_MODEL = "gpt-realtime-2.1"
_VOICE = "marin"
_TRACE = Path("tests/fakes/fixtures/realtime_trace.jsonl")
_MAX_TRACE_AUDIO_BYTES = 3_840
_SECRET_TEXT = re.compile(r"sk-[A-Za-z0-9_-]+|(?i:Bearer)\s+[^\s,;]+")
_SECRET_FIELD = re.compile(r"api[_-]?key|authorization|access[_-]?token|secret|password", re.I)


def _test_wav() -> bytes:
    """Create non-sensitive test audio: 750 ms of a quiet 440 Hz tone."""
    sample_rate = 24_000
    with io.BytesIO() as output:
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            samples = bytearray()
            for index in range(int(sample_rate * 0.75)):
                value = round(3_000 * math.sin(2 * math.pi * 440 * index / sample_rate))
                samples.extend(value.to_bytes(2, "little", signed=True))
            wav.writeframes(samples)
        return output.getvalue()


def _read_pcm(wav_data: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_data), "rb") as wav:
        if (
            wav.getnchannels() != 1
            or wav.getsampwidth() != 2
            or wav.getframerate() != 24_000
            or wav.getcomptype() != "NONE"
        ):
            raise ValueError("Trace WAV must be uncompressed PCM16 mono at 24 kHz")
        return wav.readframes(wav.getnframes())


def _sanitize(value: Any, audio_budget: list[int]) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        is_audio_delta = value.get("type") == "response.output_audio.delta"
        for key, item in value.items():
            name = str(key)
            if _SECRET_FIELD.search(name):
                cleaned[name] = "[REDACTED]"
            elif is_audio_delta and name == "delta" and isinstance(item, str):
                try:
                    audio = base64.b64decode(item, validate=True)
                except ValueError:
                    cleaned[name] = _SECRET_TEXT.sub("[REDACTED]", item)
                else:
                    allowed = min(len(audio), audio_budget[0])
                    allowed -= allowed % 2
                    audio_budget[0] -= allowed
                    cleaned[name] = base64.b64encode(audio[:allowed]).decode("ascii")
                    if allowed < len(audio):
                        cleaned["audio_truncated"] = True
            else:
                cleaned[name] = _sanitize(item, audio_budget)
        return cleaned
    if isinstance(value, list):
        return [_sanitize(item, audio_budget) for item in value]
    if isinstance(value, str):
        return _SECRET_TEXT.sub("[REDACTED]", value)
    return value


class TraceRecorder:
    def __init__(self, socket: aiohttp.ClientWebSocketResponse) -> None:
        self.socket = socket
        self.events: list[dict[str, Any]] = []

    async def send(self, event: dict[str, Any]) -> None:
        await self.socket.send_json(event)

    async def receive(self) -> dict[str, Any]:
        message = await asyncio.wait_for(self.socket.receive(), timeout=20)
        if message.type != aiohttp.WSMsgType.TEXT:
            raise RuntimeError(f"Realtime WebSocket ended unexpectedly ({message.type.name})")
        event = json.loads(message.data)
        if not isinstance(event, dict):
            raise RuntimeError("Realtime server sent a non-object event")
        self.events.append(event)
        if event.get("type") == "error":
            error = event.get("error")
            if isinstance(error, dict):
                fields = {
                    key: error[key] for key in ("type", "code", "param", "message") if key in error
                }
                safe = _sanitize(fields, [_MAX_TRACE_AUDIO_BYTES])
                raise RuntimeError(f"Realtime API error: {json.dumps(safe, ensure_ascii=False)}")
            raise RuntimeError("Realtime API returned an error event")
        return event

    async def until(self, predicate: Any) -> dict[str, Any]:
        while True:
            event = await self.receive()
            if predicate(event):
                return event


def _write_trace(events: list[dict[str, Any]]) -> None:
    _TRACE.parent.mkdir(parents=True, exist_ok=True)
    audio_budget = [_MAX_TRACE_AUDIO_BYTES]
    temporary = _TRACE.with_suffix(".jsonl.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            for event in events:
                safe = _sanitize(event, audio_budget)
                output.write(json.dumps(safe, ensure_ascii=False, allow_nan=False) + "\n")
        temporary.replace(_TRACE)
    finally:
        temporary.unlink(missing_ok=True)


async def _record(wav_data: bytes, model: str, voice: str) -> int:
    settings = Settings()
    api_key = settings.openai_api_key.get_secret_value()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is empty")
    url = f"wss://api.openai.com/v1/realtime?model={quote(model, safe='')}"
    headers = {"Authorization": f"Bearer {api_key}"}
    pcm = _read_pcm(wav_data)

    async with aiohttp.ClientSession() as client:  # noqa: SIM117
        async with client.ws_connect(url, headers=headers, heartbeat=20) as socket:
            trace = TraceRecorder(socket)
            await trace.until(lambda event: event.get("type") == "session.created")
            await trace.send(
                {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "model": model,
                        "instructions": (
                            "This is a non-sensitive protocol test. Call the ping function once, "
                            "then speak a long, simple test sentence."
                        ),
                        "output_modalities": ["audio"],
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcm", "rate": 24_000},
                                "transcription": {
                                    "model": "gpt-4o-mini-transcribe",
                                    "language": "en",
                                },
                                "turn_detection": None,
                            },
                            "output": {
                                "format": {"type": "audio/pcm", "rate": 24_000},
                                "voice": voice,
                            },
                        },
                        "tools": [
                            {
                                "type": "function",
                                "name": "ping",
                                "description": "Return a fixed protocol-test response.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {},
                                    "required": [],
                                    "additionalProperties": False,
                                },
                            }
                        ],
                        "tool_choice": "auto",
                        "max_output_tokens": 256,
                    },
                }
            )
            await trace.until(lambda event: event.get("type") == "session.updated")

            for offset in range(0, len(pcm), 960):
                frame = pcm[offset : offset + 960]
                await trace.send(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(frame).decode("ascii"),
                    }
                )
            await trace.send({"type": "input_audio_buffer.commit"})
            await trace.until(lambda event: event.get("type") == "input_audio_buffer.committed")

            await trace.send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "Please call ping once, then answer in a long test sentence."
                                ),
                            }
                        ],
                    },
                }
            )
            await trace.send({"type": "response.create"})
            tool_event = await trace.until(
                lambda event: (
                    event.get("type") == "response.output_item.done"
                    and isinstance(event.get("item"), dict)
                    and event["item"].get("type") == "function_call"
                )
            )
            item = tool_event["item"]
            if item.get("name") != "ping" or not item.get("call_id"):
                raise RuntimeError("Realtime returned an unexpected tool call")
            call_id = item["call_id"]
            await trace.send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({"ok": True, "detail": "pong"}),
                    },
                }
            )
            await trace.send({"type": "response.create"})
            audio_event = await trace.until(
                lambda event: event.get("type") == "response.output_audio.delta"
            )
            audio_item_id = audio_event.get("item_id")
            if not isinstance(audio_item_id, str):
                raise RuntimeError("Realtime audio delta did not include its item_id")

            # No audio is played by the recorder, so zero milliseconds were heard.
            await trace.send({"type": "response.cancel"})
            await trace.send(
                {
                    "type": "conversation.item.truncate",
                    "item_id": audio_item_id,
                    "content_index": 0,
                    "audio_end_ms": 0,
                }
            )
            await trace.until(lambda event: event.get("type") == "conversation.item.truncated")
            if not any(
                event.get("type") == "response.done"
                and isinstance(event.get("response"), dict)
                and event["response"].get("status") == "cancelled"
                for event in trace.events
            ):
                await trace.until(
                    lambda event: (
                        event.get("type") == "response.done"
                        and isinstance(event.get("response"), dict)
                        and event["response"].get("status") == "cancelled"
                    )
                )
            _write_trace(trace.events)
            return len(trace.events)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wav", type=Path, help="PCM16 mono 24 kHz WAV (defaults to a generated tone)"
    )
    parser.add_argument(
        "--model", default=None, help="Realtime model (defaults to config or gpt-realtime-2.1)"
    )
    parser.add_argument(
        "--voice", default=None, help="Realtime voice (defaults to config or marin)"
    )
    args = parser.parse_args()
    settings = Settings()
    model = args.model or settings.openai_realtime_model or _MODEL
    voice = args.voice or settings.openai_realtime_voice or _VOICE
    wav_data = args.wav.read_bytes() if args.wav else _test_wav()
    try:
        count = await _record(wav_data, model, voice)
    except aiohttp.WSServerHandshakeError as exc:
        raise SystemExit(f"Realtime WebSocket handshake failed: HTTP {exc.status}") from None
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise SystemExit(f"Realtime trace transport failed: {type(exc).__name__}") from None
    except RuntimeError as exc:
        safe = _SECRET_TEXT.sub("[REDACTED]", str(exc))
        raise SystemExit(safe) from None
    print(f"Recorded {count} server events in {_TRACE}; model={model}; voice={voice}")


if __name__ == "__main__":
    asyncio.run(main())
