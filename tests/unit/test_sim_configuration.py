"""Verify opt-in simulation targeting without contacting a robot."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("opt_in", "sim_marker", "expected"),
    [
        ("1", True, "webrtc://override.invalid:8443"),
        ("0", True, "webrtc://dotenv.invalid:8443"),
        ("1", False, "webrtc://dotenv.invalid:8443"),
    ],
)
def test_sim_target_override_and_offline_isolation(
    tmp_path: Path, opt_in: str, sim_marker: bool, expected: str
) -> None:
    conftest = Path(__file__).resolve().parents[1] / "conftest.py"
    (tmp_path / "conftest.py").write_text(conftest.read_text())
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    sim: simulation targeting\n")
    (tmp_path / ".env").write_text("ROBOT_LINK=webrtc://dotenv.invalid:8443\n")
    marker = "@pytest.mark.sim\n" if sim_marker else ""
    (tmp_path / "test_target.py").write_text(
        "import os\nfrom pathlib import Path\nimport pytest\n"
        "from conftest import load_sim_settings\n\n"
        + marker
        + "def test_target():\n"
        + f"    assert load_sim_settings(Path('.env')).robot_link == {expected!r}\n"
        + "    assert 'RELAY_TOKEN' not in os.environ\n"
        + "    assert 'OPENAI_API_KEY' not in os.environ\n"
    )
    env = os.environ | {
        "COINCOIN_TEST_SIM": opt_in,
        "ROBOT_LINK": "webrtc://override.invalid:8443",
        "RELAY_TOKEN": "test-only",
        "OPENAI_API_KEY": "test-only",
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
