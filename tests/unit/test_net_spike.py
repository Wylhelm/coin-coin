"""Test the Bash probe with fake Docker/IP commands, not real Docker evidence."""

import os
import shutil
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="The spike runs in WSL/Linux")

_ROOT = Path(__file__).parents[2]


@pytest.fixture
def fake_network_commands(tmp_path: Path) -> dict[str, str]:
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            """
            import os
            import subprocess
            import sys

            args = sys.argv[1:]
            if args[0] in {"info", "image"}:
                raise SystemExit(0)
            if args[0] != "run":
                raise SystemExit("Unexpected fake Docker command")
            index = args.index("-c")
            code, host, port = args[index + 1:index + 4]
            if os.environ["FAKE_SPIKE_PATH"] == "B" and host == "host.docker.internal":
                print("Fake path A is unavailable", file=sys.stderr)
                raise SystemExit(1)
            if host == "host.docker.internal":
                host = "127.0.0.1"
            name, token = args[args.index("--env") + 1].split("=", 1)
            environment = dict(os.environ, **{name: token})
            result = subprocess.run(
                [sys.executable, "-c", code, host, port], env=environment, check=False
            )
            raise SystemExit(result.returncode)
            """
        ),
        encoding="utf-8",
    )
    docker.chmod(0o755)
    ip = tmp_path / "ip"
    ip.write_text(
        f'#!{sys.executable}\nprint("2: eth0 inet 127.0.0.2/8 scope global eth0")\n',
        encoding="utf-8",
    )
    ip.chmod(0o755)
    return {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}


@pytest.mark.parametrize("path", ["A", "B"])
def test_fake_docker_paths_verify_echo_and_cleanup(
    fake_network_commands: dict[str, str], path: str
) -> None:
    environment = {**fake_network_commands, "FAKE_SPIKE_PATH": path}
    result = subprocess.run(
        ["bash", str(_ROOT / "scripts" / "net_spike.sh")],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert f"RESULT={path}:" in result.stdout
    assert "PASS: exact TCP echo" in result.stdout
    if path == "B":
        assert "PASS: fallback rejects unauthenticated connections" in result.stdout
    host = "127.0.0.1" if path == "A" else "127.0.0.2"
    with socket.socket() as connection:
        assert connection.connect_ex((host, 7899)) != 0
    assert not list((_ROOT / ".cache").glob("net-spike.*"))


def test_spike_does_not_stop_an_existing_listener(
    fake_network_commands: dict[str, str],
) -> None:
    environment = {**fake_network_commands, "FAKE_SPIKE_PATH": "A"}
    with socket.socket() as existing:
        existing.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        existing.bind(("127.0.0.1", 7899))
        existing.listen()
        result = subprocess.run(
            ["bash", str(_ROOT / "scripts" / "net_spike.sh")],
            cwd=_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 1
        assert "no existing process was stopped" in result.stderr
        with socket.create_connection(("127.0.0.1", 7899), timeout=1):
            accepted, _ = existing.accept()
            accepted.close()
    assert not list((_ROOT / ".cache").glob("net-spike.*"))


def test_bash_syntax() -> None:
    assert shutil.which("bash")
    subprocess.run(["bash", "-n", str(_ROOT / "scripts" / "net_spike.sh")], check=True)
