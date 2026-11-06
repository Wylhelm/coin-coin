#!/usr/bin/env python3
"""Run one local simulator, TCP relay and Coin-Coin; never connect to real hardware."""

from __future__ import annotations

import argparse
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

COIN = Path(__file__).resolve().parents[1]
PORTS = (7801, 7901, 7810, 7811, 8765, 8443, 8080)
LOCK = Path.home() / ".cache/coin-coin-locks/sim-camera"


def positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("duration must be finite and greater than zero")
    return seconds


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime",
        type=Path,
        default=COIN.parent / "microduck",
        help="MicroDuck runtime checkout (contains scripts/duck-sim)",
    )
    parser.add_argument(
        "--rl",
        type=Path,
        default=COIN.parent / "microduck_rl",
        help="prepared microduck_rl checkout",
    )
    parser.add_argument(
        "--language", help="default response language, e.g. English, French or auto"
    )
    parser.add_argument(
        "--max-move-s",
        type=positive_seconds,
        help="simulation movement ceiling; otherwise use .env/default (5 s)",
    )
    parser.add_argument("--headless", action="store_true", help="no MuJoCo window; keep the camera")
    parser.add_argument(
        "--check",
        action="store_true",
        help="check prerequisites and ports without starting services or calling APIs",
    )
    return parser.parse_args(argv)


def listening(port: int) -> bool:
    with socket.socket() as connection:
        connection.settimeout(0.3)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def environment(args: argparse.Namespace) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{Path.home()}/.cargo/bin:{Path.home()}/.local/bin:{env.get('PATH', '')}"
    env.update(
        DUCK_SIM_RL=str(args.rl.expanduser().resolve()),
        DUCK_SIM_PORT="7801",
        DUCK_SIM_FRAME_PORT="7901",
        DUCK_SIM_DUCKS="1",
        DUCK_SIM_DUCK="duck-a",
        DUCK_SIM_VIEWER="0" if args.headless else "1",
        DUCK_SIM_CAMERAS="a",
        DUCK_SIM_SCENE="backlash",
        DUCK_SIM_KEYFRAME="SIT",
        ROBOT_LINK="tcp://127.0.0.1:7810",
        FRAME_LINK="tcp://127.0.0.1:7811",
        RELAY_TOKEN="",
        OPENCLAW_TOKEN="",
        ECHO="0",
        LOG_AUDIO="false",
        QUACK_SINK="stream",
        WAKE_ENGINE="vosk",
        PYTHONUNBUFFERED="1",
    )
    plugin_dir = Path("/opt/gst-plugins-rs/lib/gstreamer-1.0")
    if plugin_dir.is_dir() and not env.get("GST_PLUGIN_PATH"):
        env["GST_PLUGIN_PATH"] = str(plugin_dir)
    env.setdefault("GST_PLUGIN_FEATURE_RANK", "nvh264enc:NONE")
    if args.language is not None:
        env["RESPONSE_LANGUAGE"] = args.language
    if args.max_move_s is not None:
        env["MAX_MOVE_S"] = str(args.max_move_s)
    return env


def preflight(args: argparse.Namespace, env: dict[str, str]) -> None:
    if sys.platform != "linux":
        raise RuntimeError("Run this script on Linux, or inside WSL2 on Windows.")
    required = (
        COIN / ".venv/bin/python",
        COIN / "relay/duck_relay.py",
        args.runtime.expanduser().resolve() / "scripts/duck-sim",
        args.rl.expanduser().resolve() / ".venv/bin/python",
        COIN / "assets/models/silero_vad.onnx",
        COIN / "assets/models/vosk-model-small-fr/am/final.mdl",
        COIN / "assets/models/vosk-model-small-en-us/am/final.mdl",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("Missing prerequisites:\n  " + "\n  ".join(missing))
    for tool in ("cargo", "setpriv", "gst-inspect-1.0"):
        if shutil.which(tool, path=env["PATH"]) is None:
            raise RuntimeError(f"Missing executable: {tool}. See README.md.")
    for element in ("webrtcsink", "x264enc"):
        result = subprocess.run(
            ["gst-inspect-1.0", element], env=env, capture_output=True, check=False
        )
        if result.returncode:
            raise RuntimeError(f"GStreamer element unavailable: {element}. See simulator setup.")
    occupied = [port for port in PORTS if listening(port)]
    if occupied:
        raise RuntimeError(
            f"Required ports are already in use: {occupied}; no service was stopped."
        )
    if LOCK.exists():
        raise RuntimeError(f"Simulator lock exists: {LOCK}. Stop its owner before retrying.")
    # Validate exactly the settings the child will use, without printing any secret.
    code = (
        "from coincoin.config import Settings\n"
        "s=Settings()\n"
        "if not s.openai_api_key.get_secret_value():\n"
        "    raise SystemExit('OPENAI_API_KEY is missing; configure .env or the environment')\n"
        "print('Configuration: voice=' + s.openai_realtime_voice + "
        "', response language=' + s.response_language + ', max move=' + str(s.max_move_s) + ' s')"
    )
    result = subprocess.run([str(required[0]), "-c", code], cwd=COIN, env=env, check=False)
    if result.returncode:
        raise RuntimeError("Coin-Coin configuration check failed; no services started.")
    print(
        "Prerequisites OK. OpenClaw disabled; robot/frame links forced to this local simulator.",
        flush=True,
    )


def stop_owned(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        print(f"Graceful shutdown timed out for owned PID {process.pid}; forcing exit.", flush=True)
        process.kill()
        process.wait(timeout=5)


def health_ready() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/healthz", timeout=1) as response:
            return response.status == 200 and b'"ok"' in response.read()
    except (urllib.error.URLError, TimeoutError):
        return False


def run(args: argparse.Namespace, env: dict[str, str]) -> None:
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    children: list[subprocess.Popen[bytes]] = []
    owner = uuid.uuid4().hex
    state: Path | None = None
    locked = False
    sim_started = False
    sim = args.runtime.expanduser().resolve() / "scripts/duck-sim"
    python = COIN / ".venv/bin/python"
    try:
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        LOCK.mkdir()
        locked = True
        (LOCK / "owner").write_text(owner)
        state = Path(tempfile.mkdtemp(prefix="cc-live-", dir=Path.home() / ".cache"))
        env["DUCK_SIM_STATE"] = str(state)
        print(f"State and logs: {state}", flush=True)
        print("Starting the simulator (the runtime may build its Rust binaries).", flush=True)
        sim_started = True
        # A stop requested during the simulator's startup is handled once its launcher exits,
        # so its PID files are complete before `down` performs identity-checked cleanup.
        subprocess.run(
            ["setpriv", "--no-new-privs", str(sim)],
            cwd=COIN,
            env=env,
            check=True,
            start_new_session=True,
        )
        if stopping:
            return
        for label, command, port in (
            (
                "relay",
                [
                    str(python),
                    "relay/duck_relay.py",
                    "--host",
                    "127.0.0.1",
                    "--state-dir",
                    str(state),
                ],
                7810,
            ),
            ("coincoin", [str(python), "-m", "coincoin.app", "--host", "127.0.0.1"], 8765),
        ):
            log = state / f"{label}.log"
            with log.open("ab") as output:
                child = subprocess.Popen(
                    command,
                    cwd=COIN,
                    env=env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            children.append(child)
            deadline = time.monotonic() + 60
            while not listening(port) or (label == "coincoin" and not health_ready()):
                if stopping:
                    return
                if child.poll() is not None:
                    raise RuntimeError(f"{label} exited with {child.returncode}; see {log}")
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{label} did not become ready; see {log}")
                time.sleep(0.25)
            print(f"{label} ready (PID {child.pid}); log: {log}", flush=True)
        print(
            "LIVE READY: http://localhost:8765/ -- enable the microphone and say 'Coin coin'.",
            flush=True,
        )
        print(
            "Ctrl+C stops this stack. No API request is made until you wake Coin-Coin.", flush=True
        )
        while not stopping:
            for child in children:
                if child.poll() is not None:
                    raise RuntimeError(f"Live service PID {child.pid} exited: {child.returncode}")
            time.sleep(0.25)
    finally:
        try:
            for child in reversed(children):
                stop_owned(child)
        finally:
            try:
                if sim_started:
                    subprocess.run(
                        ["setpriv", "--no-new-privs", str(sim), "down"],
                        cwd=COIN,
                        env=env,
                        check=True,
                        timeout=60,
                        start_new_session=True,
                    )
                occupied = [port for port in PORTS if listening(port)]
                if occupied:
                    raise RuntimeError(
                        f"Cleanup incomplete: ports still occupied {occupied}. "
                        f"Lock retained; inspect {state}."
                    )
                if locked and (LOCK / "owner").read_text() == owner:
                    (LOCK / "owner").unlink()
                    LOCK.rmdir()
                if locked:
                    print(f"Stack stopped; ports released. Logs retained at {state}.", flush=True)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env = environment(args)
    try:
        preflight(args, env)
        if not args.check:
            run(args, env)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
