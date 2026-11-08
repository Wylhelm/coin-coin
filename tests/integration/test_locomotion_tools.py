import json

from coincoin.robot.capabilities import PROBE_MODE
from tests.integration.test_tools_e2e_fakes import rig as rig


def mode_changes(robot):
    return [
        record.params
        for record in robot.records
        if record.method == "robot.setMode" and record.params != {"mode": PROBE_MODE}
    ]


async def test_voice_tools_switch_skate_walk_skate_over_tcp(rig):
    app, _, robot, _ = rig
    definitions = {tool["name"]: tool for tool in app.tools.definitions()}
    assert definitions["set_mode"]["parameters"]["properties"]["mode"]["enum"] == ["walk", "roller"]
    for mode, lateral in (("roller", 0), ("walk", 0.1), ("roller", 0)):
        result = await app.tools.dispatch("set_mode", json.dumps({"mode": mode}), app)
        assert result["ok"] and robot.mode == mode
        result = await app.tools.dispatch(
            "move", '{"vx":0.1,"vy":0.1,"vyaw":0,"duration_s":0.05}', app
        )
        assert result["ok"]
        completed = await app.robot.governor.wait_motion()
        assert completed is not None and completed.ok
        move = next(record for record in reversed(robot.records) if record.method == "robot.move")
        assert move.params is not None and move.params["vy"] == lateral
        assert robot.twist == [0, 0, 0]
    assert mode_changes(robot) == [{"mode": "roller"}, {"mode": "walk"}, {"mode": "roller"}]


async def test_voice_mode_rejects_bad_arguments_without_transmitting(rig):
    app, _, robot, _ = rig
    for arguments in ("{}", '{"mode":"ice"}', '{"mode":true}', '{"mode":"walk","extra":1}'):
        outcome = await app.tools.dispatch("set_mode", arguments, app)
        assert not outcome["ok"] and outcome["reason"]
    assert not mode_changes(robot)
