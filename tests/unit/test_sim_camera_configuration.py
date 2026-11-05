"""Exercise the actual opt-in camera test against an injected offline link."""

import time
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from coincoin.config import Settings
from coincoin.robot.frames import FrameError
from coincoin.robot.link import Frame, LinkDown
from tests.conftest import load_sim_settings
from tests.fakes.fake_robotd import FakeRobotLink
from tests.sim import test_sim_frame as camera


@pytest.mark.parametrize("scheme", ["tcp", "webrtc"])
@pytest.mark.parametrize("failure", [None, "connect", "frame"])
async def test_camera_uses_explicit_target_and_reports_unavailable_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scheme: str, failure: str | None
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ROBOT_LINK=tcp://dotenv.invalid:7930\nFRAME_LINK=tcp://frame.invalid:7931\n"
    )
    target = f"{scheme}://override.invalid:8443"
    monkeypatch.setenv("COINCOIN_TEST_SIM", "1")
    monkeypatch.setenv("ROBOT_LINK", target)
    monkeypatch.setattr(camera, "ENV_FILE", env_file)
    assert load_sim_settings(env_file).robot_link == target
    settings_seen: list[Settings] = []
    events: list[str] = []
    image = BytesIO()
    Image.new("RGB", (32, 24), "green").save(image, format="JPEG")

    class CameraLink(FakeRobotLink):
        async def connect(self) -> None:
            events.append("connect")
            if failure == "connect":
                raise LinkDown("offline camera connection unavailable")

        async def frame(self) -> Frame:
            events.append("frame")
            if failure == "frame":
                raise FrameError("offline camera unavailable")
            return Frame(image.getvalue(), 32, 24, time.time())

        async def close(self) -> None:
            events.append("close")

    link = CameraLink()

    def factory(url, *, frame_url, token):
        assert url == target
        assert frame_url == "tcp://frame.invalid:7931"
        assert token == ""
        settings_seen.append(load_sim_settings(env_file))
        return link

    monkeypatch.setattr(camera, "create_robot_link", factory)
    expected_error = (
        LinkDown
        if failure == "connect" and scheme == "webrtc"
        else FrameError
        if failure == "frame"
        else None
    )
    if expected_error is None:
        await camera.test_real_camera_frame_saved_and_removed()
    else:
        with pytest.raises(expected_error, match="offline camera"):
            await camera.test_real_camera_frame_saved_and_removed()
    assert len(settings_seen) == 1
    assert events[-1] == "close"
    assert ("connect" in events) == (scheme == "webrtc")
    if failure == "connect" and scheme == "webrtc":
        assert "frame" not in events
    else:
        assert "frame" in events
