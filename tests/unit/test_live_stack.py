"""Launcher safety and lifecycle tests; no real simulator, ports or APIs."""

import argparse
import subprocess
from pathlib import Path

import pytest

from scripts import live_stack


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_invalid_move_ceiling_is_rejected(value: str) -> None:
    with pytest.raises(SystemExit):
        live_stack.parse_args(["--max-move-s", value])


def test_language_and_duration_are_opt_in_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBOT_LINK", "tcp://hardware.example:7810")
    monkeypatch.setenv("FRAME_LINK", "tcp://hardware.example:7811")
    monkeypatch.setenv("OPENCLAW_TOKEN", "must-not-be-used")
    default = live_stack.environment(live_stack.parse_args([]))
    assert default["ROBOT_LINK"] == "tcp://127.0.0.1:7810"
    assert default["FRAME_LINK"] == "tcp://127.0.0.1:7811"
    assert default["OPENCLAW_TOKEN"] == default["RELAY_TOKEN"] == ""
    assert "RESPONSE_LANGUAGE" not in default
    assert "MAX_MOVE_S" not in default
    custom = live_stack.environment(
        live_stack.parse_args(["--language", "French", "--max-move-s", "30"])
    )
    assert custom["RESPONSE_LANGUAGE"] == "French"
    assert custom["MAX_MOVE_S"] == "30.0"


def test_custom_scene_is_opt_in_and_resolved_from_invocation_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DUCK_SIM_SCENE", "untrusted-environment")
    default = live_stack.environment(live_stack.parse_args([]))
    assert default["DUCK_SIM_SCENE"] == "backlash"
    assert default["DUCK_SIM_PRELOAD_EQUIPMENT"] == "0"
    custom = live_stack.environment(live_stack.parse_args(["--scene", "my room/scene.xml"]))
    assert custom["DUCK_SIM_SCENE"] == str(tmp_path / "my room/scene.xml")
    assert custom["DUCK_SIM_PRELOAD_EQUIPMENT"] == "0"
    preloaded = live_stack.environment(
        live_stack.parse_args(["--scene", "my room/scene.xml", "--preload-equipment"])
    )
    assert preloaded["DUCK_SIM_PRELOAD_EQUIPMENT"] == "1"


def test_places_enable_simulator_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NAV_MAP", "untrusted")
    default = live_stack.environment(live_stack.parse_args([]))
    assert "NAV_MAP" not in default and "SIM_POSE_LINK" not in default
    args = live_stack.parse_args(["--places", "nav.json"])
    env = live_stack.environment(args)
    assert env["NAV_MAP"] == str(tmp_path / "nav.json")
    assert env["SIM_POSE_LINK"] == f"tcp://127.0.0.1:{env['DUCK_SIM_PORT']}"
    assert env["TOF_LINK"] == "tcp://127.0.0.1:7812"
    assert env["NAV_POSE"] == "estimated" and env["NAV_START"] == ""
    env = live_stack.environment(
        live_stack.parse_args(["--places", "nav.json", "--start", "départ@0", "--oracle-pose"])
    )
    assert env["NAV_START"] == "départ@0" and env["NAV_POSE"] == "oracle"
    monkeypatch.setattr(live_stack.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="Navigation map does not exist"):
        live_stack.preflight(args, env)


def test_missing_custom_scene_fails_before_starting_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live_stack.sys, "platform", "linux")
    args = live_stack.parse_args(["--scene", str(tmp_path / "missing.xml")])
    with pytest.raises(RuntimeError, match="Custom simulator scene does not exist"):
        live_stack.preflight(args, live_stack.environment(args))
    assert list(tmp_path.iterdir()) == []


def test_invalid_custom_scene_reports_compile_error_before_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coin = tmp_path / "coin"
    runtime = tmp_path / "runtime"
    rl = tmp_path / "rl"
    scene = tmp_path / "scene.xml"
    scene.write_text("invalid")
    for path in (
        coin / ".venv/bin/python",
        coin / "relay/duck_relay.py",
        runtime / "scripts/duck-sim",
        rl / ".venv/bin/python",
        coin / "assets/models/silero_vad.onnx",
        coin / "assets/models/vosk-model-small-fr/am/final.mdl",
        coin / "assets/models/vosk-model-small-en-us/am/final.mdl",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setattr(live_stack, "COIN", coin)
    monkeypatch.setattr(live_stack.sys, "platform", "linux")
    commands: list[list[str]] = []

    def fail_compile(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "invalid MJCF")

    monkeypatch.setattr(live_stack.subprocess, "run", fail_compile)
    args = live_stack.parse_args(
        ["--scene", str(scene), "--runtime", str(runtime), "--rl", str(rl)]
    )
    with pytest.raises(RuntimeError, match="invalid MJCF"):
        live_stack.preflight(args, live_stack.environment(args))
    assert len(commands) == 1
    assert commands[0][-1] == str(scene)


def test_check_mode_does_not_start_services(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_stack, "preflight", lambda *_: None)

    def forbidden(*_):
        pytest.fail("--check started services")

    monkeypatch.setattr(live_stack, "run", forbidden)
    assert live_stack.main(["--check"]) == 0


def test_missing_prerequisites_fail_without_creating_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live_stack, "COIN", tmp_path)
    monkeypatch.setattr(live_stack.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="Missing prerequisites"):
        live_stack.preflight(live_stack.parse_args([]), {})
    assert list(tmp_path.iterdir()) == []


class OwnedProcess:
    pid = 12345

    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, *, timeout):
        return self.returncode


@pytest.fixture
def launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cache = tmp_path / ".cache"
    cache.mkdir()
    monkeypatch.setattr(live_stack.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(live_stack, "LOCK", cache / "locks/sim-camera")
    monkeypatch.setattr(live_stack, "COIN", tmp_path)
    commands: list[list[str]] = []
    children: list[OwnedProcess] = []

    def command(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def start(argv, **kwargs):
        process = OwnedProcess()
        children.append(process)
        if len(children) == 2:
            process.returncode = 4
        return process

    monkeypatch.setattr(live_stack.subprocess, "run", command)
    monkeypatch.setattr(live_stack.subprocess, "Popen", start)
    monkeypatch.setattr(live_stack, "health_ready", lambda: True)
    monkeypatch.setattr(live_stack, "listening", lambda port: len(commands) == 1)
    args = argparse.Namespace(runtime=tmp_path, rl=tmp_path)
    return args, commands, children


def test_service_failure_stops_only_owned_services_and_releases_lock(launcher) -> None:
    args, commands, children = launcher
    with pytest.raises(RuntimeError, match="exited: 4"):
        live_stack.run(args, {})
    assert children[0].terminated
    assert not children[1].terminated  # already exited
    assert commands[-1][-1] == "down"
    assert not live_stack.LOCK.exists()


def test_failed_simulator_start_still_runs_cleanup(launcher, monkeypatch: pytest.MonkeyPatch):
    args, commands, children = launcher

    def command(argv, **kwargs):
        commands.append(argv)
        if argv[-1] != "down":
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(live_stack.subprocess, "run", command)
    with pytest.raises(subprocess.CalledProcessError):
        live_stack.run(args, {})
    assert commands[-1][-1] == "down"
    assert not children
    assert not live_stack.LOCK.exists()


def test_existing_lock_is_never_deleted(launcher) -> None:
    args, commands, _ = launcher
    live_stack.LOCK.mkdir(parents=True)
    owner = live_stack.LOCK / "owner"
    owner.write_text("someone-else")
    with pytest.raises(FileExistsError):
        live_stack.run(args, {})
    assert owner.read_text() == "someone-else"
    assert not commands


def test_cleanup_failure_retains_the_lock(launcher, monkeypatch: pytest.MonkeyPatch):
    args, _, _ = launcher
    monkeypatch.setattr(live_stack, "listening", lambda port: True)
    with pytest.raises(RuntimeError, match="Cleanup incomplete"):
        live_stack.run(args, {})
    assert live_stack.LOCK.exists()


def test_interrupt_cleans_up_and_restores_signal_handlers(
    launcher, monkeypatch: pytest.MonkeyPatch
):
    args, commands, children = launcher
    before = live_stack.signal.getsignal(live_stack.signal.SIGINT)

    def start(argv, **kwargs):
        process = OwnedProcess()
        children.append(process)
        return process

    def interrupt(_seconds):
        handler = live_stack.signal.getsignal(live_stack.signal.SIGINT)
        assert callable(handler)
        handler(live_stack.signal.SIGINT, None)

    monkeypatch.setattr(live_stack.subprocess, "Popen", start)
    monkeypatch.setattr(live_stack.time, "sleep", interrupt)
    live_stack.run(args, {})
    assert len(children) == 2 and all(process.terminated for process in children)
    assert commands[-1][-1] == "down"
    assert not live_stack.LOCK.exists()
    assert live_stack.signal.getsignal(live_stack.signal.SIGINT) == before


def test_stubborn_owned_process_is_killed_after_timeout(monkeypatch: pytest.MonkeyPatch):
    process = OwnedProcess()
    waits = 0

    def wait(*, timeout):
        nonlocal waits
        waits += 1
        if waits == 1:
            raise subprocess.TimeoutExpired("owned-service", timeout)
        return process.returncode

    monkeypatch.setattr(process, "wait", wait)
    live_stack.stop_owned(process)  # type: ignore[arg-type]
    assert process.terminated and process.killed
