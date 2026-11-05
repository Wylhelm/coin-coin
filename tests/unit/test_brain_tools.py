import asyncio
import json
from pathlib import Path
from typing import Any

from coincoin.brain.base import NullBrain
from coincoin.brain.feature import BrainFeature
from coincoin.brain.openclaw import OpenClawArchivedSessionError, OpenClawUnavailableError
from coincoin.brain.summary import read_journal_conversation
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.realtime.tools.brain import MAX_FACT_CHARS, MAX_QUESTION_CHARS, brain_tools


class _Brain:
    def __init__(self, *, remember_error: BaseException | None = None) -> None:
        self.remember_error = remember_error
        self.last_failure = None
        self.asked: list[str] = []
        self.remembered: list[str] = []

    async def session_start(self) -> str:
        raise RuntimeError("unexpected client state")

    async def ask(self, message: str) -> str:
        self.asked.append(message)
        return "Réponse."

    async def remember(self, fact: str) -> None:
        if self.remember_error is not None:
            raise self.remember_error
        self.remembered.append(fact)

    async def session_end(self, summary: str) -> None:
        _ = summary


def _tools(brain: Any, failures: list[tuple[str, str]], slow: list[float], times: list[float]):
    ticks = iter(times)
    ask, remember = brain_tools(
        brain,
        on_failure=lambda operation, kind: failures.append((operation, kind)),
        on_slow_ask=slow.append,
        clock=lambda: next(ticks),
    )
    return ask.handler, remember.handler


async def test_tools_validate_bounded_text_arguments() -> None:
    brain = _Brain()
    ask, remember = _tools(brain, [], [], [])

    for arguments in ({}, {"question": 3}, {"question": "  "}):
        assert (await ask(None, arguments))["ok"] is False
    assert (await ask(None, {"question": "x" * (MAX_QUESTION_CHARS + 1)}))["ok"] is False
    assert (await remember(None, {"fact": ""}))["ok"] is False
    assert (await remember(None, {"fact": "x" * (MAX_FACT_CHARS + 1)}))["ok"] is False
    assert brain.asked == [] and brain.remembered == []


async def test_slow_ask_is_reported_after_one_and_a_half_seconds() -> None:
    slow: list[float] = []
    ask, _ = _tools(_Brain(), [], slow, [10.0, 11.4, 20.0, 21.6])

    assert await ask(None, {"question": "Rapide ?"}) == {"ok": True, "detail": "Réponse."}
    assert slow == []
    await ask(None, {"question": "Lente ?"})
    assert slow == [21.6 - 20.0]


async def test_remember_maps_brain_failures_to_spoken_reasons() -> None:
    failures: list[tuple[str, str]] = []
    _, archived = _tools(
        _Brain(remember_error=OpenClawArchivedSessionError("409")), failures, [], []
    )
    _, down = _tools(_Brain(remember_error=OpenClawUnavailableError("503")), failures, [], [])
    _, slow = _tools(_Brain(remember_error=TimeoutError()), failures, [], [])
    _, offline = _tools(NullBrain(), failures, [], [])

    assert await archived(None, {"fact": "f"}) == {
        "ok": False,
        "reason": "Ma mémoire est archivée ; il faut la restaurer.",
    }
    assert (await down(None, {"fact": "f"}))["reason"] == "Ma mémoire ne répond pas pour le moment."
    assert (await slow(None, {"fact": "f"}))["reason"] == "Ma mémoire ne répond pas pour le moment."
    assert (await offline(None, {"fact": "f"}))["ok"] is False
    assert failures == [
        ("remember", "archived"),
        ("remember", "unavailable"),
        ("remember", "unavailable"),
    ]


def test_journal_reader_keeps_only_this_conversation(tmp_path: Path) -> None:
    journal = Journal(log_dir=tmp_path, session_id="process")
    other = Journal(log_dir=tmp_path, session_id="other-process")
    journal.write("user_transcript", text="Avant le réveil.")
    journal.write("brain", operation="session_start", conversation="c1")
    other.write("user_transcript", text="Autre processus.")
    journal.write("user_transcript", text="Bonjour.")
    journal.write("assistant_transcript", text="Coin !")
    journal.write("tool_result", name="remember", result={"ok": True, "detail": "stored"})
    journal.write("tool_result", name="move", result={"ok": False, "reason": "STOP"})
    journal.write("tool_result", name="odd", result="text")
    with (tmp_path / "2000-01-01.jsonl").open("w") as stale:
        stale.write("not json\n" + json.dumps(["list"]) + "\n")

    transcripts, actions = read_journal_conversation(tmp_path, "process", "c1")

    assert transcripts == ["Utilisateur : Bonjour.", "Coin-Coin : Coin !"]
    assert actions == [
        "remember → stored",
        "move → échec : STOP",
        "odd → résultat invalide",
    ]


def _journal_events(log_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in log_dir.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]


class _App:
    def __init__(self, tmp_path: Path) -> None:
        self.settings = Settings.model_construct()
        self.journal = Journal(log_dir=tmp_path)
        self.context: dict[str, Any] = {}
        self.handlers: dict[str, Any] = {}
        self.tools: list[str] = []

    def instructions_context(self, key: str, provider: Any) -> None:
        self.context[key] = provider

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    def register_tool(self, name: str, description: str, schema: Any, handler: Any) -> None:
        _ = description, schema, handler
        self.tools.append(name)


async def test_unexpected_start_failure_still_yields_memoryless_context(tmp_path: Path) -> None:
    app = _App(tmp_path)
    feature = BrainFeature(_Brain())
    await feature.install(app)  # type: ignore[arg-type]

    assert app.tools == ["ask_openclaw", "remember"]
    assert app.context["memory_context"]() == "none"
    await app.handlers["brain_session_start"]()

    assert "memory is unavailable" in app.context["memory_context"]()
    app.handlers["brain_session_end"](summary="")
    await asyncio.wait_for(feature.aclose(), timeout=1)
    events = _journal_events(tmp_path)
    assert {"brain"} <= {event["type"] for event in events}
    assert any(event.get("status") == "empty" for event in events)
