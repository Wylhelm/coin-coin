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
