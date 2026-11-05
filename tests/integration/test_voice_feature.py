import asyncio
import struct
import wave
from pathlib import Path

from coincoin.app import App
from coincoin.audio.io import PlaybackProgress
from coincoin.clock import FakeClock
from coincoin.journal import Journal
from coincoin.robot.feature import RobotFeature
from coincoin.robot.voice_feature import VoiceFeature
from coincoin.statemachine import Event, Mode
from tests.fakes.fake_robotd import FakeRobotLink
from tests.integration.test_s4_regressions import _Audio, _Session, _settings


def _sound_bank(directory: Path, *tags: str) -> None:
    for tag in tags:
        target = directory / tag
        target.mkdir(parents=True)
        with wave.open(str(target / "test.wav"), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24_000)
            wav.writeframes(struct.pack("<h", 8192) * 4_800)


async def _install_app(
    tmp_path: Path, audio: _Audio, clock: FakeClock, bank: Path
) -> tuple[App, FakeRobotLink, _Session]:
    settings = _settings()
    session = _Session()
    app = App(
        settings,
        audio,
        Journal(tmp_path / "logs"),
        session_factory=lambda _settings: session,  # type: ignore[arg-type]
        clock=clock,
        features=(),
    )
    link = FakeRobotLink(clock)
    robot = RobotFeature(link=link)
    await robot.install(app)
    await VoiceFeature(bank).install(app)
    assert robot.runtime is not None
    return app, link, session


async def _wait_quack(app: App, audio: _Audio, expected: int, clock: FakeClock) -> None:
    for _ in range(100):
        if audio.progress >= expected and not app.state.quack_active:
            await asyncio.sleep(0)
            if audio.progress >= expected and not app.state.quack_active:
                return
        clock.advance(0.02)
        await asyncio.sleep(0)
    raise AssertionError(
        f"audio progress did not reach {expected} ms; got {audio.progress}, state={app.state}"
    )


async def test_voice_feature_automatic_quacks_and_inquire_cooldown(tmp_path: Path) -> None:
    bank = tmp_path / "sounds"
    _sound_bank(bank, "greet", "inquire", "coo")
    clock = FakeClock(10)
    audio = _Audio()
    app, link, session = await _install_app(tmp_path, audio, clock, bank)
    try:
        await app._apply(Event("wake"))
        await session.open_started.wait()
        await _wait_quack(app, audio, 200, clock)
        assert app.mode == Mode.AWAKE

        await app._apply(Event("response_done", question=True))
        await _wait_quack(app, audio, 400, clock)
        one_inquire = audio.progress
        await app._apply(Event("response_done", question=True))
        for _ in range(10):
            await asyncio.sleep(0)
        assert audio.progress == one_inquire

        clock.advance(30)
        await app._apply(Event("response_done", question=True))
        await _wait_quack(app, audio, one_inquire + 200, clock)
        assert audio.progress == one_inquire + 200

        await app._apply(Event("sleep_requested"))
        await _wait_quack(app, audio, one_inquire + 400, clock)
        assert app.mode == Mode.SLEEPING
        assert audio.progress == one_inquire + 400
        mouth = [record.params for record in link.records if record.method == "robot.mouth"]
        assert mouth[-1] == {"open": 0.0}
    finally:
        await app.stop()


class _BlockingAudio(_Audio):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.epoch = 0
        self.sent = 0
        self.played = 0
        self.flushes = 0

    @property
    def playback_progress(self) -> PlaybackProgress:
        return PlaybackProgress(self.epoch, self.sent, self.played)

    async def play(self, pcm: bytes) -> None:
        epoch = self.epoch
        self.started.set()
        await self.release.wait()
        if epoch == self.epoch:
            self.sent += len(pcm) // 2
            self.played = self.sent
            self.progress += len(pcm) // 48

    async def flush(self) -> None:
        self.flushes += 1
        self.epoch += 1
        self.sent = self.played = self.progress = 0
        self.release.set()


async def test_barge_in_and_stop_cancel_quack_and_close_mouth(tmp_path: Path) -> None:
    bank = tmp_path / "sounds"
    _sound_bank(bank, "greet")
    clock = FakeClock()
    audio = _BlockingAudio()
    app, link, session = await _install_app(tmp_path, audio, clock, bank)
    try:
        await app._apply(Event("wake"))
        await session.open_started.wait()
        await audio.started.wait()
        await app._apply(Event("user_speech_start"))
        assert audio.flushes == 1
        assert app.mode == Mode.AWAKE
        assert not app.state.speaking
        assert any(
            record.method == "robot.mouth" and record.params == {"open": 0.0}
            for record in link.records
        )

        await app.stop_pressed()
        assert app.mode == Mode.SLEEPING
        assert audio.flushes == 2
        assert any(
            record.method == "robot.mouth" and record.params == {"open": 0.0}
            for record in link.records
        )
    finally:
        await app.stop()


async def test_missing_bank_keeps_app_awake_without_quack_audio(tmp_path: Path) -> None:
    clock = FakeClock()
    audio = _Audio()
    app, _, session = await _install_app(tmp_path, audio, clock, tmp_path / "absent")
    try:
        await app._apply(Event("wake"))
        await session.open_started.wait()
        assert app.mode == Mode.AWAKE
        assert audio.progress == 0
    finally:
        await app.stop()


async def test_voice_installer_creates_runtime_per_app(tmp_path: Path) -> None:
    bank = tmp_path / "sounds"
    _sound_bank(bank, "greet")
    clock = FakeClock()
    audio_a, audio_b = _Audio(), _Audio()
    app_a = App(_settings(), audio_a, Journal(tmp_path / "a"), features=(), clock=clock)
    app_b = App(_settings(), audio_b, Journal(tmp_path / "b"), features=(), clock=clock)
    robot_a, robot_b = RobotFeature(FakeRobotLink(clock)), RobotFeature(FakeRobotLink(clock))
    installer = VoiceFeature(bank)
    try:
        await robot_a.install(app_a)
        await installer.install(app_a)
        await robot_b.install(app_b)
        await installer.install(app_b)
        assert app_a.audio is not app_b.audio
        assert app_a.robot is not None and app_b.robot is not None
        assert app_a.robot.quack is not app_b.robot.quack
    finally:
        await app_a.stop()
        await app_b.stop()
