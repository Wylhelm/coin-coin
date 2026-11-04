#!/usr/bin/env bash
# Bounded WSL -> Docker TCP probe. Own only port 7899 and the recorded echo PID.
set -Eeuo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
port=7899
image=python:3.12-slim
echo_pid=""
work_dir=""

stop_echo() {
    if [[ -n "$echo_pid" ]]; then
        if kill -0 "$echo_pid" 2>/dev/null; then
            kill "$echo_pid"
        fi
        wait "$echo_pid" 2>/dev/null || :
        echo_pid=""
    fi
}

cleanup() {
    stop_echo
    if [[ -n "$work_dir" ]]; then
        rm -f -- "$work_dir/echo.log"
        rmdir -- "$work_dir"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for tool in python3 docker; do
    if ! command -v "$tool" >/dev/null; then
        printf 'BLOCKED: missing %s in WSL\n' "$tool" >&2
        exit 1
    fi
done
if ! docker info >/dev/null; then
    printf 'BLOCKED: Docker Desktop engine is not available from WSL\n' >&2
    exit 1
fi
if ! docker image inspect "$image" >/dev/null 2>&1; then
    if ! docker pull "$image"; then
        printf 'BLOCKED: cannot fetch the Python image required for a network measurement\n' >&2
        exit 1
    fi
fi

mkdir -p -- "$project_dir/.cache"
work_dir="$(mktemp -d "$project_dir/.cache/net-spike.XXXXXX")"

start_echo() {
    local bind_host="$1" token="$2"
    COINCOIN_SPIKE_TOKEN="$token" python3 -u - "$bind_host" "$port" >"$work_dir/echo.log" 2>&1 <<'PY' &
import asyncio
import hmac
import logging
import os
import sys

logging.basicConfig(level=logging.INFO)
token = os.environ["COINCOIN_SPIKE_TOKEN"].encode()


async def echo(reader, writer):
    try:
        if token:
            received = await asyncio.wait_for(reader.readline(), timeout=3)
            if not hmac.compare_digest(received.rstrip(b"\r\n"), token):
                logging.warning("Network spike rejected an unauthenticated connection")
                return
        while data := await reader.readline():
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def main():
    server = await asyncio.start_server(echo, sys.argv[1], int(sys.argv[2]))
    print("READY", flush=True)
    async with server:
        await server.serve_forever()


asyncio.run(main())
PY
    echo_pid=$!
    for ((attempt = 0; attempt < 50; attempt++)); do
        if grep -qx READY "$work_dir/echo.log"; then
            return 0
        fi
        if ! kill -0 "$echo_pid" 2>/dev/null; then
            cat "$work_dir/echo.log" >&2
            printf 'BLOCKED: cannot bind echo on %s:%s; no existing process was stopped\n' \
                "$bind_host" "$port" >&2
            return 1
        fi
        sleep 0.1
    done
    cat "$work_dir/echo.log" >&2
    printf 'BLOCKED: echo startup exceeded five seconds\n' >&2
    return 1
}

client_code="$(cat <<'PY'
import os
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
token = os.environ["COINCOIN_SPIKE_TOKEN"]
payload = b"coincoin-network-spike\n"
if token:
    with socket.create_connection((host, port), timeout=3) as connection:
        connection.sendall(payload)
        if connection.recv(64) != b"":
            raise SystemExit("FAIL: fallback accepted a connection without its token")
    print("PASS: fallback rejects unauthenticated connections")
with socket.create_connection((host, port), timeout=3) as connection:
    if token:
        connection.sendall(token.encode() + b"\n")
    connection.sendall(payload)
    with connection.makefile("rb") as reader:
        if reader.readline(64) != payload:
            raise SystemExit("FAIL: echoed bytes differ from the payload")
print("PASS: exact TCP echo from the Docker container")
PY
)"

probe() {
    docker run --rm --add-host=host.docker.internal:host-gateway \
        --env "COINCOIN_SPIKE_TOKEN=$2" "$image" \
        python -c "$client_code" "$1" "$port"
}

printf 'Testing A: host.docker.internal -> WSL 127.0.0.1:%s\n' "$port"
start_echo 127.0.0.1 ""
if probe host.docker.internal ""; then
    printf 'RESULT=A: use localhost WSL via host.docker.internal; RELAY_TOKEN is empty\n'
    exit 0
fi
printf 'A failed; testing B on eth0 with a temporary shared token\n' >&2
stop_echo

if ! command -v ip >/dev/null; then
    printf 'BLOCKED: missing ip command required to discover WSL eth0\n' >&2
    exit 1
fi
wsl_ip="$(ip -4 -o addr show dev eth0 | awk '{split($4, address, "/"); print address[1]; exit}')"
if [[ -z "$wsl_ip" ]]; then
    printf 'BLOCKED: WSL eth0 has no IPv4 address\n' >&2
    exit 1
fi
token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
start_echo "$wsl_ip" "$token"
if probe "$wsl_ip" "$token"; then
    printf 'RESULT=B: use tcp://%s:<relay-port>, bind eth0, require RELAY_TOKEN on the first line\n' \
        "$wsl_ip"
else
    printf 'BLOCKED: both measured Docker -> WSL network paths failed\n' >&2
    exit 1
fi
