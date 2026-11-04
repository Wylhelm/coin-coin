import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretBytes, SecretStr

from coincoin.journal import Journal


def test_jsonl_metadata_and_append(tmp_path: Path) -> None:
    now = datetime(2026, 11, 4, 12, tzinfo=UTC)
    journal = Journal(tmp_path, session_id="test-session", now=lambda: now)
    journal.write("wake", detail="hello", count=1)
    journal.write("latency", ms=42)
    lines = (tmp_path / "2026-11-04.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {
        "ts": "2026-11-04T12:00:00+00:00",
        "session_id": "test-session",
        "type": "wake",
        "detail": "hello",
        "count": 1,
    }
    assert json.loads(lines[1])["ms"] == 42


def test_recursive_key_pattern_and_header_redaction(tmp_path: Path) -> None:
    api_key = "sk-" + "A" * 20
    journal = Journal(tmp_path)
    journal.write(
        "error",
        nested={
            "API_KEY": "example-api",
            "accessToken": "example-token",
            "my_secret": "example-secret",
            "Authorization": "Bearer example-auth",
            "PASSWORD": "example-password",
            "items": [{"text": f"Failed with {api_key}."}, ("safe", {"token": "hidden"})],
        },
        detail="Authorization: Bearer example-inline",
        secret_object=SecretStr("example-object"),
        value=SecretStr("example-value"),
        binary_secret=SecretBytes(b"example-binary"),
    )
    content = next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8")
    for secret in (
        api_key,
        "example-api",
        "example-token",
        "example-secret",
        "example-auth",
        "example-password",
        "example-inline",
        "example-object",
        "example-value",
        "example-binary",
        "hidden",
    ):
        assert secret not in content
    event = json.loads(content)
    assert event["nested"]["items"][0]["text"] == "Failed with [REDACTED]."
    assert event["nested"]["items"][1] == ["safe", {"token": "[REDACTED]"}]
    assert event["detail"] == "[REDACTED]"
    assert event["value"] == "[REDACTED]"


def test_date_rotation_uses_utc(tmp_path: Path) -> None:
    current = datetime(2026, 11, 4, 23, 59, tzinfo=UTC)
    journal = Journal(tmp_path, now=lambda: current)
    journal.write("state")
    current += timedelta(minutes=2)
    journal.write("state")
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "2026-11-04.jsonl",
        "2026-11-05.jsonl",
    ]
    current = datetime(2026, 11, 6, 1, tzinfo=timezone(timedelta(hours=2)))
    journal.write("state")
    assert len((tmp_path / "2026-11-05.jsonl").read_text().splitlines()) == 2


def test_retention_only_purges_older_daily_files(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    for name in ("2026-10-04.jsonl", "2026-10-05.jsonl", "2026-11-04.jsonl", "notes.jsonl"):
        (tmp_path / name).write_text("{}\n", encoding="utf-8")
    (tmp_path / "2026-13-04.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "2020-01-01.jsonl").mkdir()
    Journal(tmp_path, retention_days=30, now=lambda: datetime(2026, 11, 4, tzinfo=UTC))
    assert not (tmp_path / "2026-10-04.jsonl").exists()
    assert (tmp_path / "2026-10-05.jsonl").exists()
    assert (tmp_path / "2026-11-04.jsonl").exists()
    assert (tmp_path / "notes.jsonl").exists()
    assert (tmp_path / "2020-01-01.jsonl").is_dir()
    assert "invalid daily journal filename" in caplog.text


@pytest.mark.parametrize("metadata", [{"ts": "fake"}, {"session_id": "fake"}])
def test_metadata_cannot_be_overridden(tmp_path: Path, metadata: dict[str, Any]) -> None:
    journal = Journal(tmp_path)
    with pytest.raises(ValueError, match="metadata"):
        journal.write("wake", **metadata)
    assert not list(tmp_path.glob("*.jsonl"))


def test_invalid_event_data_does_not_create_a_log(tmp_path: Path) -> None:
    journal = Journal(tmp_path)
    with pytest.raises(TypeError):
        journal.write("error", audio=b"not-for-the-journal")
    with pytest.raises(ValueError):
        journal.write("latency", value=float("nan"))
    with pytest.raises(ValueError):
        journal.write("")
    assert not list(tmp_path.glob("*.jsonl"))


def test_invalid_retention_and_naive_time_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Retention"):
        Journal(tmp_path, retention_days=0)
    with pytest.raises(ValueError, match="timezone-aware"):
        Journal(tmp_path, now=lambda: datetime(2026, 11, 4))


def test_io_errors_are_not_silenced(tmp_path: Path) -> None:
    occupied = tmp_path / "not-a-directory"
    occupied.write_text("occupied", encoding="utf-8")
    with pytest.raises(FileExistsError):
        Journal(occupied)
