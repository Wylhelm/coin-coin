"""Realtime memory tools backed by the configured OpenClaw brain."""

import time
from collections.abc import Callable
from typing import Any

from coincoin.brain.base import Brain, NullBrain
from coincoin.brain.openclaw import SPOKEN_FAILURES, FailureKind, OpenClawError, failure_kind
from coincoin.realtime.tools import ToolDefinition

MAX_QUESTION_CHARS = 2_000
MAX_FACT_CHARS = 500
SLOW_ASK_S = 1.5
NO_MEMORY = "Je n'ai pas de mémoire : OpenClaw n'est pas configuré."

FailureReporter = Callable[[str, FailureKind], None]
SlowReporter = Callable[[float], None]

ASK_DESCRIPTION = (
    "Ask Coin-Coin's long-term memory agent (OpenClaw) about user, past conversations, "
    "or a question needing long reasoning. It may take several seconds: in the same response, "
    "first say one very short waiting phrase in the user's language such as « je réfléchis… », "
    "then call this tool. Repeat the answer briefly; if it says memory is unavailable, say so."
)
REMEMBER_DESCRIPTION = (
    "Store one durable fact the user explicitly wants Coin-Coin to remember for future "
    "conversations, for example their name or a preference. Use a short third-person fact."
)


def _text_argument(arguments: dict[str, Any], key: str, limit: int) -> str | None:
    value = arguments.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= limit else None


def brain_tools(
    brain: Brain,
    *,
    on_failure: FailureReporter,
    on_slow_ask: SlowReporter,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[ToolDefinition, ...]:
    """Build the ask_openclaw and remember tools bound to one brain instance."""

    async def ask_openclaw(context: object, arguments: dict[str, Any]) -> dict[str, Any]:
        question = _text_argument(arguments, "question", MAX_QUESTION_CHARS)
        if question is None:
            return {
                "ok": False,
                "reason": f"question must be non-empty text of at most {MAX_QUESTION_CHARS} chars",
            }
        started = clock()
        answer = await brain.ask(question)
        elapsed = clock() - started
        if elapsed > SLOW_ASK_S:
            on_slow_ask(elapsed)
        failure: FailureKind | None = getattr(brain, "last_failure", None)
        if failure is not None:
            on_failure("ask_openclaw", failure)
            return {"ok": False, "reason": answer}
        return {"ok": True, "detail": answer}

    async def remember(context: object, arguments: dict[str, Any]) -> dict[str, Any]:
        fact = _text_argument(arguments, "fact", MAX_FACT_CHARS)
        if fact is None:
            return {
                "ok": False,
                "reason": f"fact must be non-empty text of at most {MAX_FACT_CHARS} chars",
            }
        if isinstance(brain, NullBrain):
            return {"ok": False, "reason": NO_MEMORY}
        try:
            await brain.remember(fact)
        except (OpenClawError, TimeoutError) as exc:
            kind = failure_kind(exc)
            on_failure("remember", kind)
            return {"ok": False, "reason": SPOKEN_FAILURES[kind]}
        return {"ok": True, "detail": "Fact stored in long-term memory"}

    return (
        ToolDefinition(
            "ask_openclaw",
            ASK_DESCRIPTION,
            {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "maxLength": MAX_QUESTION_CHARS},
                },
                "required": ["question"],
                "additionalProperties": False,
            },
            ask_openclaw,
        ),
        ToolDefinition(
            "remember",
            REMEMBER_DESCRIPTION,
            {
                "type": "object",
                "properties": {"fact": {"type": "string", "maxLength": MAX_FACT_CHARS}},
                "required": ["fact"],
                "additionalProperties": False,
            },
            remember,
        ),
    )
