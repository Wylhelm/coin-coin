"""Opt-in real camera capture, without sending an image to OpenAI."""

import os
import tempfile
import time
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from pydantic_settings import DotEnvSettingsSource

from coincoin.config import Settings
from coincoin.realtime.tools.vision import prepare_image
from coincoin.robot.frames import FrameError
from coincoin.robot.tcp_link import TcpLink

pytestmark = pytest.mark.sim
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


async def test_real_camera_frame_saved_and_removed():
    if os.environ.get("COINCOIN_TEST_SIM") != "1":
        pytest.skip("Set COINCOIN_TEST_SIM=1 with your assigned camera relay")
    settings = Settings.model_validate(DotEnvSettingsSource(Settings, env_file=ENV_FILE)())
    link = TcpLink(
        settings.robot_link,
        frame_url=settings.frame_link,
        token=settings.relay_token.get_secret_value(),
    )
    try:
        try:
            frame = await link.frame()
        except FrameError:
            pytest.skip("The explicitly assigned camera is unavailable")
        assert 0 <= time.time() - frame.captured_at <= 2
        jpeg = prepare_image(frame.jpeg)
        image = Image.open(BytesIO(jpeg))
        assert image.width <= 640 and len(jpeg) <= 150_000
        assert (frame.width, frame.height) == Image.open(BytesIO(frame.jpeg)).size
        with tempfile.TemporaryDirectory(prefix="coincoin-s7-frame-") as directory:
            path = Path(directory) / "frame.jpg"
            path.write_bytes(jpeg)
            with Image.open(path) as saved:
                saved.verify()
            print(f"camera: {image.width}x{image.height}, {len(jpeg)} bytes, JPEG verified")
        assert not path.exists()
    finally:
        await link.close()
