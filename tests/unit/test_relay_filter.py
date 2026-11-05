import json
import logging
from pathlib import Path

import pytest

from relay.duck_relay import FRAME_METHODS, MAX_LINE, ROBOT_METHODS, DuckRelay, filter_line


@pytest.mark.parametrize(
    ("methods", "method"),
    [(ROBOT_METHODS, name) for name in ROBOT_METHODS]
    + [(FRAME_METHODS, name) for name in FRAME_METHODS],
)
def test_every_permitted_method_passes_exactly(methods, method):
    wire = json.dumps({"jsonrpc": "2.0", "id": 42, "method": method, "params": {}}).encode() + b"\n"
    assert filter_line(wire, methods) == (wire, None)


@pytest.mark.parametrize(
    "method",
    [
        "robot.init",
        "robot.relax",
        "robot.rest",
        "robot.shutdown",
        "robot.rebootMotors",
        "robot.setMode",
        "robot.setSkill",
        "robot.removeSkill",
        "robot.loadPolicy",
        "robot.reloadPolicies",
        "robot.theremin",
        "robot.chorale",
        "robot.flashlight",
        "robot.vetbook.get",
        "robot.state",
        *[
            f"{family}.test"
            for family in (
                "policy",
                "detector",
                "account",
                "os",
                "telemetry",
                "update",
                "system",
                "net",
                "pad",
            )
        ],
        "media.frame",
    ],
)
def test_forbidden_families_refused(method):
    wire = json.dumps({"jsonrpc": "2.0", "id": "test", "method": method}).encode()
    forward, error = filter_line(wire, ROBOT_METHODS)
    assert forward is None
    assert error is not None
    response = json.loads(error)
    assert response["id"] == "test"
    assert response["error"]["code"] == -32601
    assert "duck-relay" in response["error"]["message"]


def test_frame_port_cannot_move_robot():
    _, error = filter_line(b'{"jsonrpc":"2.0","id":1,"method":"robot.move"}', FRAME_METHODS)
    assert error is not None
    assert json.loads(error)["error"]["code"] == -32601


@pytest.mark.parametrize(
    "line",
    [
        b"invalid",
        b"\xff",
        b"[]",
        b'[{"jsonrpc":"2.0","id":1,"method":"hello"}]',
        b'{"jsonrpc":"2.0","id":7}',
        b'{"jsonrpc":"2.0","id":true,"method":"hello"}',
        b'{"jsonrpc":"2.0","id":{},"method":"hello"}',
        b'{"jsonrpc":"1.0","method":"hello"}',
        b'{"jsonrpc":"2.0","method":null}',
        b'{"jsonrpc":"2.0","method":""}',
        b'{"jsonrpc":"2.0","method":"hello","params":1}',
        b'{"jsonrpc":"2.0","method":"hello","params":{"x":NaN}}',
        b'{"jsonrpc":"2.0","id":1e999,"method":"hello"}',
        b"x" * (MAX_LINE + 1),
    ],
)
def test_invalid_envelopes(line):
    forward, error = filter_line(line, ROBOT_METHODS)
    assert forward is None
    assert error is not None
    assert json.loads(error)["error"]["code"] == -32600


def test_invalid_request_keeps_valid_id():
    _, error = filter_line(b'{"jsonrpc":"2.0","id":7}', ROBOT_METHODS)
    assert error is not None
    assert json.loads(error)["id"] == 7
    forward, _ = filter_line(b'{"jsonrpc":"2.0","id":1.5,"method":"hello"}', ROBOT_METHODS)
    assert forward is not None


def test_refused_notification_is_dropped_without_params_in_log(caplog):
    caplog.set_level(logging.INFO)
    assert filter_line(
        b'{"jsonrpc":"2.0","method":"robot.relax","params":{"token":"test-private"}}',
        ROBOT_METHODS,
    ) == (None, None)
    assert "robot.relax" in caplog.text
    assert "test-private" not in caplog.text
    assert "token" not in caplog.text


@pytest.mark.parametrize(
    "kwargs",
    [{"host": "0.0.0.0"}, {"token": "bad\n"}, {"token": "bad\r"}, {"duck": "../duck-a"}],
)
def test_unsafe_configuration_refused(tmp_path: Path, kwargs):
    with pytest.raises(ValueError):
        DuckRelay(tmp_path, **kwargs)
