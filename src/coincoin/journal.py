"""Daily UTC JSONL journal with recursive secret redaction and retention."""

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import SecretBytes, SecretStr

_SECRET_KEY = re.compile(r"key|token|secret|authorization|password", re.IGNORECASE)
_API_KEY = re.compile(r"sk-[A-Za-z0-9_-]+")
_AUTH_HEADER = re.compile(
    r"\bauthorization\s*[:=]\s*(?:(?:bearer|basic)\s+)?[^\s,;]+", re.IGNORECASE
)
_DAILY_FILE = re.compile(r"\d{4}-\d{2}-\d{2}\.jsonl")
_REDACTED = "[REDACTED]"
_LOGGER = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _redact(value: Any) -> Any:
    if isinstance(value, (SecretStr, SecretBytes)):
        return _REDACTED
    if isinstance(value, dict):
        return {
            key: _REDACTED if _SECRET_KEY.search(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _AUTH_HEADER.sub(_REDACTED, _API_KEY.sub(_REDACTED, value))
    return value


class Journal:
    """Append metadata-protected, redacted JSON events to daily UTC files.

    Bytes (including audio) are not JSON-serializable and are never persisted.
    I/O and serialization errors propagate instead of silently losing events.
    """

    def __init__(
        self,
        log_dir: Path = Path("logs"),
        *,
        session_id: str | None = None,
        retention_days: int = 30,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        if retention_days < 1:
            raise ValueError("Retention must be at least one day")
        self.log_dir = log_dir
        self.session_id = session_id if session_id is not None else str(uuid4())
        self._now = now
        self.log_dir.mkdir(parents=True, exist_ok=True)
        cutoff = self._timestamp().date() - timedelta(days=retention_days)
        for path in self.log_dir.glob("*.jsonl"):
            if not path.is_file() or not _DAILY_FILE.fullmatch(path.name):
                continue
            try:
                file_date = date.fromisoformat(path.stem)
            except ValueError:
                _LOGGER.warning("Ignoring invalid daily journal filename: %s", path.name)
                continue
            if file_date < cutoff:
                path.unlink()

    def _timestamp(self) -> datetime:
        timestamp = self._now()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Journal timestamps must be timezone-aware")
        return timestamp.astimezone(UTC)

    def write(self, type: str, **data: Any) -> None:
        """Write one redacted event, adding ts (ISO 8601 UTC) and session_id."""
        if not type:
            raise ValueError("Journal event type must not be empty")
        if {"ts", "session_id"} & data.keys():
            raise ValueError("Journal metadata cannot be overridden")
        timestamp = self._timestamp()
        event = _redact(
            {"ts": timestamp.isoformat(), "session_id": self.session_id, "type": type, **data}
        )
        line = json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n"
        with (self.log_dir / f"{timestamp.date().isoformat()}.jsonl").open(
            "a", encoding="utf-8"
        ) as output:
            output.write(line)
