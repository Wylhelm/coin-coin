"""Local, bounded conversation summary construction."""

import json
from collections.abc import Sequence
from pathlib import Path


def build_session_summary(
    transcripts: Sequence[str],
    actions: Sequence[str],
    *,
    max_chars: int = 1500,
) -> str:
    """Keep the newest transcript/action context within the OpenClaw prompt limit."""
    if max_chars < 1:
        raise ValueError("Summary limit must be positive")
    entries = [f"Conversation: {text.strip()}" for text in transcripts if text.strip()]
    entries.extend(f"Action: {action.strip()}" for action in actions if action.strip())
    if not entries:
        return ""
    summary = "\n".join(entries)
    if len(summary) <= max_chars:
        return summary
    return "…" + summary[-(max_chars - 1) :] if max_chars > 1 else "…"


def read_journal_conversation(
    log_dir: Path, session_id: str, conversation_id: str
) -> tuple[list[str], list[str]]:
    """Return final transcripts and tool actions journaled after one conversation marker."""
    transcripts: list[str] = []
    actions: list[str] = []
    started = False
    for path in sorted(log_dir.glob("*.jsonl")):
        with path.open(encoding="utf-8") as lines:
            for line in lines:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict) or event.get("session_id") != session_id:
                    continue
                kind = event.get("type")
                if kind == "brain" and event.get("conversation") == conversation_id:
                    started = True
                    continue
                if not started:
                    continue
                if kind == "user_transcript" and isinstance(event.get("text"), str):
                    transcripts.append(f"Utilisateur : {event['text']}")
                elif kind == "assistant_transcript" and isinstance(event.get("text"), str):
                    transcripts.append(f"Coin-Coin : {event['text']}")
                elif kind == "tool_result" and isinstance(event.get("name"), str):
                    actions.append(_action(event["name"], event.get("result")))
    return transcripts, actions


def _action(name: str, result: object) -> str:
    if not isinstance(result, dict):
        return f"{name} → résultat invalide"
    if result.get("ok") is True:
        return f"{name} → {result.get('detail', 'ok')}"
    return f"{name} → échec : {result.get('reason', 'inconnu')}"
