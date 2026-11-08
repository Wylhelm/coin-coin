import json
from pathlib import Path

import pytest

from coincoin.cli import execute, main, parser
from coincoin.config import Settings
from coincoin.robot.jsonrpc import encode_request
from tests.fakes.fake_robotd import FakeRobotLink


@pytest.mark.parametrize(
    ("argv", "method", "params"),
    [
        # Raised to the walking gait's minimum (min_vx 0.3, capped by max_vx 0.25).
        (["move", "0.1", "0", "0", "0.1"], "robot.move", {"vx": 0.25, "vy": 0.0, "vyaw": 0.0}),
        (["turn", "0"], "robot.stop", None),
        (["look", "haut"], "robot.look", {"x": 1.0, "y": 0.0, "z": 0.6}),
        (
            ["pose", "0.005", "0.1", "0"],
            "robot.pose",
            {"z": 0.005, "roll": 0.1, "pitch": 0.0, "active": True},
        ),
        (["sit"], "robot.do", {"skill": "sit_toggle"}),
        (["stand"], "robot.policies", None),
        (["mode", "roller"], "robot.setMode", {"mode": "roller"}),
        (["do", "ground_pick"], "robot.do", {"skill": "ground_pick"}),
        (["skills"], "robot.skills", None),
        (["status"], "robot.health", None),
        (["stop"], "robot.stop", None),
        (["raw", "robot.health", "{}"], "robot.health", {}),
    ],
)
async def test_cli_commands_go_through_expected_rpc(
    tmp_path: Path, monkeypatch, argv, method, params
):
    monkeypatch.chdir(tmp_path)
    link = FakeRobotLink()
    output, code = await execute(parser().parse_args(argv), Settings(), link=link)
    assert code == 0 and output["ok"]
    assert any(record.method == method and record.params == params for record in link.records)
    assert not any(record.method == "robot.init" for record in link.records)
    assert not link.connected
    if argv[0] == "move":
        assert "state_before" in output and "state_after" in output


@pytest.mark.parametrize(
    "argv", [["raw", "hello", "[]"], ["raw", "hello", "bad-json"], ["do", "crouch"]]
)
async def test_cli_reports_invalid_input(tmp_path: Path, monkeypatch, argv):
    monkeypatch.chdir(tmp_path)
    output, code = await execute(parser().parse_args(argv), Settings(), link=FakeRobotLink())
    assert code == 1 and not output["ok"] and output["reason"]


def test_json_rpc_mapping_distinguishes_notifications():
    assert json.loads(encode_request(None, "robot.move", {"vx": 0.1})) == {
        "jsonrpc": "2.0",
        "method": "robot.move",
        "params": {"vx": 0.1},
    }
    assert json.loads(encode_request(1, "hello", {"api_version": 47}))["id"] == 1
    with pytest.raises(ValueError):
        encode_request(1, "robot.move", {"vx": float("nan")})


def test_cli_main_exit_code_and_json(monkeypatch, capsys):
    async def fake_execute(args, settings):
        return {"ok": False, "code": -32601, "reason": "refusee par duck-relay"}, 1

    monkeypatch.setattr("coincoin.cli.execute", fake_execute)
    with pytest.raises(SystemExit) as exited:
        main(["raw", "robot.relax"])
    assert exited.value.code == 1
    assert json.loads(capsys.readouterr().out)["code"] == -32601
