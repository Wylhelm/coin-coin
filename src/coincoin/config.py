"""Environment settings shared by every Coin-Coin implementation."""

from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Load the master specification's settings from the environment and .env.

    Realtime defaults use S4's model and the voice selected for live audition.
    The S3 Vosk threshold is the measured best-compromise setting, not a
    validated production threshold.
    Empty credentials are valid for the offline foundation and its fake services.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        allow_inf_nan=False,
    )

    openai_api_key: SecretStr = SecretStr("")
    openai_realtime_model: str = "gpt-realtime-2.1"
    openai_realtime_voice: str = "verse"
    response_language: str = Field(default="auto", min_length=1, max_length=80)
    openclaw_url: str = "http://127.0.0.1:18789"
    openclaw_token: SecretStr = SecretStr("")
    openclaw_agent: str = "coin-coin"
    robot_link: str = "tcp://host.docker.internal:7810"
    frame_link: str = "tcp://host.docker.internal:7811"
    quack_sink: Literal["stream", "robot"] = "stream"
    wake_engine: Literal["vosk", "oww"] = "vosk"
    wake_threshold: float = Field(default=0.9, ge=0, le=1)
    followup_s: float = Field(default=420, gt=0)
    langs: str = "fr,en"
    max_vx: float = Field(default=0.25, gt=0)
    max_vy: float = Field(default=0.2, gt=0)
    max_vyaw: float = Field(default=1.0, gt=0)
    max_move_s: float = Field(default=5, gt=0)
    move_hz: float = Field(default=20, gt=0)
    # Simulator aid: open walking moves with a stronger starter and raise slow requests to what
    # the walk policy actually follows. Off by default: the starter briefly exceeds max_vx.
    walk_assist: bool = False
    # Simulator-only room navigation: a map from salon_scan/build_navigation.py and the body
    # server that reports the duck's true pose. Both empty disables the go_to tool.
    nav_map: str = ""
    sim_pose_link: str = ""
    log_audio: bool = False
    log_retention_days: int = Field(default=30, gt=0)
    relay_token: SecretStr = SecretStr("")

    @field_validator("response_language")
    @classmethod
    def nonempty_response_language(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("RESPONSE_LANGUAGE must be a language name or auto")
        return value
