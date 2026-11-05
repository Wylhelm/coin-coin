"""Keep offline tests independent of a developer's real credentials."""

import os
from pathlib import Path

import pytest
from pydantic_settings import DotEnvSettingsSource

import coincoin.app as app_module
from coincoin.config import Settings


def load_sim_settings(env_file: Path) -> Settings:
    values = DotEnvSettingsSource(Settings, env_file=env_file)()
    robot_link = os.environ.get("ROBOT_LINK")
    if robot_link is not None:
        values["robot_link"] = robot_link
    return Settings.model_validate(values)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    monkeypatch.setattr(app_module, "FEATURES", [])
    sim_opt_in = (
        os.environ.get("COINCOIN_TEST_SIM") == "1"
        and request.node.get_closest_marker("sim") is not None
    )
    for field in Settings.model_fields:
        if sim_opt_in and field == "robot_link":
            monkeypatch.delenv(field, raising=False)
            continue
        monkeypatch.delenv(field.upper(), raising=False)
        monkeypatch.delenv(field, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
