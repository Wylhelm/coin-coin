from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from coincoin.config import Settings


def test_master_defaults() -> None:
    settings = Settings()
    assert settings.model_dump(exclude={"openai_api_key", "openclaw_token", "relay_token"}) == {
        "openai_realtime_model": "gpt-realtime-2.1",
        "openai_realtime_voice": "verse",
        "response_language": "auto",
        "openclaw_url": "http://127.0.0.1:18789",
        "openclaw_agent": "coin-coin",
        "robot_link": "tcp://host.docker.internal:7810",
        "frame_link": "tcp://host.docker.internal:7811",
        "quack_sink": "stream",
        "wake_engine": "vosk",
        "wake_threshold": 0.9,
        "followup_s": 420,
        "langs": "fr,en",
        "max_vx": 0.25,
        "max_vy": 0.2,
        "max_vyaw": 1.0,
        "max_move_s": 5,
        "move_hz": 20,
        "log_audio": False,
        "log_retention_days": 30,
    }
    for secret in (settings.openai_api_key, settings.openclaw_token, settings.relay_token):
        assert isinstance(secret, SecretStr)
        assert secret.get_secret_value() == ""


def test_dotenv_and_environment_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "OPENAI_API_KEY=example-api\nOPENCLAW_TOKEN=example-brain\n"
        "RELAY_TOKEN=example-relay\nFOLLOWUP_S=10\nWAKE_THRESHOLD=0.7\n"
        "COINCOIN_PORT=8770\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(Settings.model_config, "env_file", dotenv)
    monkeypatch.setenv("FOLLOWUP_S", "12")
    settings = Settings()
    assert settings.followup_s == 12
    assert settings.wake_threshold == 0.7
    assert settings.openai_api_key.get_secret_value() == "example-api"
    assert settings.openclaw_token.get_secret_value() == "example-brain"
    assert settings.relay_token.get_secret_value() == "example-relay"
    for secret in ("example-api", "example-brain", "example-relay"):
        assert secret not in repr(settings)
        assert secret not in settings.model_dump_json()


def test_example_dotenv_has_master_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    example = Path(__file__).parents[2] / ".env.example"
    monkeypatch.setitem(Settings.model_config, "env_file", example)
    assert Settings() == Settings.model_construct()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FOLLOWUP_S", "0"),
        ("MAX_VX", "-1"),
        ("MAX_VY", "nan"),
        ("MAX_VYAW", "inf"),
        ("MAX_MOVE_S", "0"),
        ("MOVE_HZ", "-1"),
        ("LOG_RETENTION_DAYS", "0"),
        ("WAKE_THRESHOLD", "1.1"),
        ("WAKE_ENGINE", "unknown"),
        ("QUACK_SINK", "unknown"),
        ("RESPONSE_LANGUAGE", ""),
        ("RESPONSE_LANGUAGE", "  "),
    ],
)
def test_invalid_configuration_is_explicit(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()
