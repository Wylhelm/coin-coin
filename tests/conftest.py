"""Keep offline tests independent of a developer's real credentials."""

import pytest

from coincoin.config import Settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
        monkeypatch.delenv(field, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
