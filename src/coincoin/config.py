"""Environment settings shared by every Coin-Coin implementation."""

from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
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
    # Smallest non-zero walking speeds: the walk policy (set v9) stands still below about
    # vx 0.2 m/s and vyaw 0.8 rad/s, so slower requests are raised to these (never above the
    # max_* caps) and the tool result says so. 0 disables a floor. Not applied on skates.
    min_vx: float = Field(default=0.3, ge=0)
    min_vy: float = Field(default=0.15, ge=0)
    min_vyaw: float = Field(default=1.0, ge=0)
    move_hz: float = Field(default=20, gt=0)
    # Simulator aid: open walking moves with a stronger starter and raise slow requests to what
    # the walk policy actually follows. Off by default: the starter briefly exceeds max_vx.
    walk_assist: bool = False
    # Room navigation: a map from salon_scan/build_navigation.py; empty disables the go_to tool.
    # The pose navigation steers by is `estimated` (MCL from odometry and the ToF depth stream at
    # tof_link; docs/localization.md) or `oracle` (the simulator body server's true pose at
    # sim_pose_link, evaluation only). With `estimated`, sim_pose_link is only used for scoring.
    # nav_start: a place, place@heading_deg, or x,y,heading_deg; empty means unknown.
    nav_map: str = ""
    nav_pose: Literal["estimated", "oracle"] = "estimated"
    nav_start: str = ""
    # With no usable estimate the duck sweeps the head and matches the whole flat, and refuses
    # honestly when two places fit equally well (docs/localization.md). False restores the strict
    # old behaviour: an unknown start is refused instead of searched for.
    nav_global_relocalization: bool = True
    # tof_link: the relay's tofd port (tcp://host:7812), or `link` to subscribe to tof.stream
    # over the WebRTC robot link itself (mediad routes it to tofd).
    tof_link: str = ""
    sim_pose_link: str = ""
    log_audio: bool = False
    log_retention_days: int = Field(default=30, gt=0)
    relay_token: SecretStr = SecretStr("")
    # Connection profile. Empty keeps ROBOT_LINK as configured. sim and robot both use mediad's
    # WebRTC path (the one the delivered robot exposes), so the simulator exercises it exactly.
    robot_target: Literal["", "sim", "robot"] = ""
    robot_host: str = ""
    robot_signalling_port: int = Field(default=8443, gt=0, lt=65536)
    # A person must fit/remove skates when the link refuses robot.setMode (WebRTC); how long
    # Coin-Coin waits for robot.mode to change before giving up. Motion stays disabled meanwhile.
    person_mode_timeout_s: float = Field(default=120, gt=0)
    # Local simulator-only assistant; never used unless robotd explicitly reports simulated.
    sim_equipment_socket: str = ""

    @model_validator(mode="after")
    def apply_robot_target(self) -> "Settings":
        if not self.robot_target:
            return self
        host = self.robot_host.strip()
        if not host:
            if self.robot_target == "robot":
                raise ValueError(
                    "ROBOT_TARGET=robot needs ROBOT_HOST (the duck's IP: run `duckctl ip`)"
                )
            host = "127.0.0.1"
        if any(character in host for character in "/?#@ ") or host.count(":") == 1:
            raise ValueError("ROBOT_HOST must be a host name or IP address, without port or path")
        literal = f"[{host}]" if ":" in host else host
        self.robot_host = host
        self.robot_link = f"webrtc://{literal}:{self.robot_signalling_port}"
        return self

    @field_validator("response_language")
    @classmethod
    def nonempty_response_language(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("RESPONSE_LANGUAGE must be a language name or auto")
        return value
