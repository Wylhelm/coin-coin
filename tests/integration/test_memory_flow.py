import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr

from coincoin.app import App
from coincoin.brain.feature import BrainFeature
from coincoin.brain.openclaw import OpenClawBrain
from coincoin.brain.summary import build_session_summary
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.realtime.events import (
    RealtimeEvent,
    ResponseDone,
    ResponseStarted,
    ToolCall,
    UserTranscript,
)
from coincoin.statemachine import Event, Mode, State
from tests.fakes.fake_openclaw import FakeOpenClaw

_TOKEN = "loopback-only-test-token"


async def test_memory_prompts_share_one_openclaw_session_and_local_summary() -> None:
    fake = FakeOpenClaw(content="user aime le français.")
    client = TestClient(TestServer(fake.app))
    await client.start_server()
    brain = OpenClawBrain(
        Settings(
            openclaw_url=str(client.make_url("")).rstrip("/"),
            openclaw_token=SecretStr(_TOKEN),
            openclaw_agent="coin-coin",
        )
    )
    try:
        assert await brain.session_start() == "user aime le français."
        await brain.remember("Je m'appelle user.")
        assert await brain.ask("Comment est-ce que je m'appelle ?") == (
            "user aime le français."
        )
        summary = build_session_summary(["Je m'appelle user."], ["remember"])
        await brain.session_end(summary)

        assert len(fake.requests) == 4
        assert {request["body"]["user"] for request in fake.requests} == {"coin-coin-voice"}
        prompts = [request["body"]["messages"][0]["content"] for request in fake.requests]
        assert prompts[0].startswith("[Coin-Coin se réveille]")
        assert prompts[1] == "[À mémoriser] Je m'appelle user."
        assert prompts[2] == "Comment est-ce que je m'appelle ?"
        assert prompts[3].startswith("[Fin de conversation] Résumé :")
    finally:
        await brain.close()
        await client.close()


class _SilentAudio:
    def mic_frames(self) -> AsyncIterator[bytes]:
        async def frames() -> AsyncIterator[bytes]:
            await asyncio.Future()
            yield b""

        return frames()

    async def play(self, pcm: bytes) -> None:
        _ = pcm

    async def flush(self) -> None:
        return None

    @property
    def played_ms(self) -> int:
        return 0

    @property
    def playing(self) -> bool:
        return False


class _ScriptedSession:
    """Loopback Realtime session double driven by queued events."""

    def __init__(self) -> None:
        self.opened = asyncio.Event()
        self.instructions = ""
        self.tool_names: list[str] = []
        self.tool_results: list[tuple[str, dict[str, object]]] = []
        self.responses_created = 0
        self.session_expires_at = None
        self._events: asyncio.Queue[RealtimeEvent | None] = asyncio.Queue()

    async def open(self, instructions: str, tools: list[dict[str, object]]) -> None:
        self.instructions = instructions
        self.tool_names = [str(tool["name"]) for tool in tools]
        self.opened.set()

    async def events(self) -> AsyncIterator[RealtimeEvent]:
        while (event := await self._events.get()) is not None:
            yield event

    def push(self, *events: RealtimeEvent) -> None:
        for event in events:
            self._events.put_nowait(event)

    async def send_audio(self, pcm: bytes) -> None:
        _ = pcm

    async def cancel(self) -> None:
        return None

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        _ = item_id, audio_end_ms

    async def send_tool_result(self, call_id: str, result: str) -> None:
        self.tool_results.append((call_id, json.loads(result)))

    async def create_response(self) -> None:
        self.responses_created += 1

    async def close(self) -> None:
        self._events.put_nowait(None)


class _Harness:
    def __init__(self, tmp_path: Path, settings: Settings, feature: BrainFeature) -> None:
        self.sessions: list[_ScriptedSession] = []
        self.feature = feature
        self.log_dir = tmp_path / "logs"

        def factory(_settings: Settings) -> _ScriptedSession:
            session = _ScriptedSession()
            self.sessions.append(session)
            return session

        self.app = App(
            settings,
            _SilentAudio(),  # type: ignore[arg-type]
            Journal(log_dir=self.log_dir),
            session_factory=factory,  # type: ignore[arg-type]
            features=[feature],
        )

    async def install(self) -> None:
        for feature in self.app._features:
            await feature.install(self.app)

    async def wake(self) -> _ScriptedSession:
        count = len(self.sessions)
        if self.app.mode == Mode.SLEEPING:
            # Skip the post-sleep quack cooldown that real playback would complete.
            self.app.state = State(followup_s=self.app.settings.followup_s)
        await self.app._apply(Event("wake"))
        for _ in range(500):
            if len(self.sessions) > count and self.app._session is self.sessions[-1]:
                return self.sessions[-1]
            await asyncio.sleep(0.01)
        raise AssertionError("Realtime session did not open")

    async def call_tool(
        self, session: _ScriptedSession, name: str, arguments: dict[str, str], call_id: str
    ) -> dict[str, object]:
        response_id = f"response-{call_id}"
        session.push(
            ResponseStarted(response_id=response_id),
            ToolCall(
                call_id=call_id,
                name=name,
                arguments=json.dumps(arguments),
                response_id=response_id,
            ),
            ResponseDone(status="completed", response_id=response_id),
        )
        for _ in range(1_000):
            for result_id, result in session.tool_results:
                if result_id == call_id:
                    return result
            await asyncio.sleep(0.01)
        raise AssertionError(f"Tool {name} returned no result")

    def journal_text(self) -> str:
        return "".join(path.read_text() for path in sorted(self.log_dir.glob("*.jsonl")))

    def journal_events(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.journal_text().splitlines()]

    async def close(self) -> None:
        await self.app.stop_pressed()
        for task in tuple(self.app._tasks):
            task.cancel()
        if self.app._tasks:
            await asyncio.gather(*tuple(self.app._tasks), return_exceptions=True)
        await self.feature.aclose()


def _openclaw_settings(url: str, token: str = _TOKEN) -> Settings:
    return Settings.model_construct(
        openai_api_key=SecretStr("local-test-key"),
        openai_realtime_model="gpt-realtime-2.1",
        openai_realtime_voice="marin",
        openclaw_url=url,
        openclaw_token=SecretStr(token),
        openclaw_agent="coin-coin",
    )


async def _serve(fake: FakeOpenClaw) -> TestClient:
    client = TestClient(TestServer(fake.app))
    await client.start_server()
    return client


async def test_app_remembers_across_sleep_with_non_blocking_session_end(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeOpenClaw(
        content="Aucun souvenir.",
        expected_token=_TOKEN,
        prompt_delays={"[Fin de conversation]": 0.5},
    )
    client = await _serve(fake)
    harness = _Harness(
        tmp_path,
        _openclaw_settings(str(client.make_url("")).rstrip("/")),
        BrainFeature(),
    )
    try:
        await harness.install()
        first = await harness.wake()
        assert {"ask_openclaw", "remember", "sleep"} <= set(first.tool_names)
        assert "Notes from OpenClaw memory" in first.instructions
        assert "Aucun souvenir." in first.instructions

        first.push(
            UserTranscript(
                item_id="user-1", text="Souviens-toi que je m'appelle user.", final=True
            )
        )
        stored = await harness.call_tool(
            first, "remember", {"fact": "L'utilisateur s'appelle user."}, "call-1"
        )
        assert stored == {"ok": True, "detail": "Fact stored in long-term memory"}
        assert first.responses_created == 1

        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(harness.app.request_sleep(), timeout=0.3)
        assert loop.time() - started < 0.3
        assert harness.app.mode == Mode.SLEEPING
        assert harness.feature._tasks, "session_end must still be running in the background"

        await asyncio.wait_for(harness.feature.drain(), timeout=3)
        end_prompt = fake.requests[-1]["body"]["messages"][0]["content"]
        assert end_prompt.startswith("[Fin de conversation] Résumé : ")
        assert "Utilisateur : Souviens-toi que je m'appelle user." in end_prompt
        assert "remember → Fact stored in long-term memory" in end_prompt
        assert len(end_prompt) < 1_600

        second = await harness.wake()
        assert "L'utilisateur s'appelle user." in second.instructions
        answer = await harness.call_tool(
            second, "ask_openclaw", {"question": "Comment je m'appelle ?"}, "call-2"
        )
        assert answer == {"ok": True, "detail": "Aucun souvenir."}
        assert {request["body"]["user"] for request in fake.requests} == {"coin-coin-voice"}
        assert {request["body"]["model"] for request in fake.requests} == {"openclaw/coin-coin"}
        assert _TOKEN not in harness.journal_text()
        assert _TOKEN not in caplog.text
    finally:
        await harness.close()
        await client.close()


async def test_unavailable_gateway_keeps_conversation_in_memoryless_mode(
    tmp_path: Path,
) -> None:
    fake = FakeOpenClaw(status=503)
    client = await _serve(fake)
    harness = _Harness(
        tmp_path, _openclaw_settings(str(client.make_url("")).rstrip("/")), BrainFeature()
    )
    try:
        await harness.install()
        session = await harness.wake()
        assert "memory is unavailable" in session.instructions
        answer = await harness.call_tool(
            session, "ask_openclaw", {"question": "De quoi parlions-nous ?"}, "call-1"
        )
        assert answer == {"ok": False, "reason": "Ma mémoire ne répond pas pour le moment."}
        assert session.responses_created == 1
        assert harness.app.mode != Mode.SLEEPING
        brain_events = [e for e in harness.journal_events() if e["type"] == "brain"]
        assert any(e.get("status") == "unavailable" for e in brain_events)
    finally:
        await harness.close()
        await client.close()


async def test_rejected_token_is_explicit_and_never_journaled(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeOpenClaw(expected_token="the-right-token")
    client = await _serve(fake)
    harness = _Harness(
        tmp_path, _openclaw_settings(str(client.make_url("")).rstrip("/")), BrainFeature()
    )
    try:
        await harness.install()
        session = await harness.wake()
        assert "ma mémoire est fermée à clé" in session.instructions
        stored = await harness.call_tool(session, "remember", {"fact": "Un fait."}, "call-1")
        assert stored == {"ok": False, "reason": "Ma mémoire est fermée à clé."}
        errors = [e for e in harness.journal_events() if e["type"] == "error"]
        assert any("fermée à clé" in str(e.get("message")) for e in errors)
        assert _TOKEN not in harness.journal_text()
        assert _TOKEN not in caplog.text
    finally:
        await harness.close()
        await client.close()


async def test_archived_session_at_sleep_is_journaled_explicitly(tmp_path: Path) -> None:
    fake = FakeOpenClaw(statuses=[200, 409])
    client = await _serve(fake)
    harness = _Harness(
        tmp_path, _openclaw_settings(str(client.make_url("")).rstrip("/")), BrainFeature()
    )
    try:
        await harness.install()
        session = await harness.wake()
        session.push(UserTranscript(item_id="user-1", text="Bonne nuit.", final=True))
        for _ in range(100):
            if "Bonne nuit." in harness.journal_text():
                break
            await asyncio.sleep(0.01)
        await harness.app.request_sleep()
        await asyncio.wait_for(harness.feature.drain(), timeout=3)
        errors = [e for e in harness.journal_events() if e["type"] == "error"]
        assert any("archivée (HTTP 409)" in str(e.get("message")) for e in errors)
    finally:
        await harness.close()
        await client.close()


class _HangingBrain:
    def __init__(self) -> None:
        self.ended: list[str] = []

    async def session_start(self) -> str:
        await asyncio.Future()
        return ""

    async def ask(self, message: str) -> str:
        return message

    async def remember(self, fact: str) -> None:
        _ = fact

    async def session_end(self, summary: str) -> None:
        self.ended.append(summary)


async def test_slow_memory_start_does_not_hold_the_realtime_session(tmp_path: Path) -> None:
    brain = _HangingBrain()
    harness = _Harness(
        tmp_path,
        _openclaw_settings("http://127.0.0.1:9"),
        BrainFeature(brain, start_timeout_s=0.05),
    )
    try:
        await harness.install()
        session = await asyncio.wait_for(harness.wake(), timeout=1)
        assert "memory is unavailable" in session.instructions
    finally:
        await harness.close()


async def test_unconfigured_memory_is_honest_and_persists_nothing(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, _openclaw_settings("http://127.0.0.1:9", token=""), BrainFeature())
    try:
        await harness.install()
        session = await harness.wake()
        assert "OpenClaw n'est pas configure" in session.instructions
        stored = await harness.call_tool(session, "remember", {"fact": "Un fait."}, "call-1")
        assert stored == {
            "ok": False,
            "reason": "Je n'ai pas de mémoire : OpenClaw n'est pas configuré.",
        }
        await harness.app.request_sleep()
        assert not harness.feature._tasks
    finally:
        await harness.close()
