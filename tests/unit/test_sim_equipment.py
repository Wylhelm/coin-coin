import json
from pathlib import Path

import pytest

from coincoin.robot import sim_equipment
from coincoin.robot.link import RobotRefused


@pytest.mark.parametrize("simulated", [True, False, None, "true", 1])
async def test_local_equipment_requires_explicit_simulation(monkeypatch, simulated):
    requests = []

    class Writer:
        closed = False

        def write(self, data):
            requests.append(json.loads(data))

        async def drain(self):
            pass

        def close(self):
            self.closed = True

        async def wait_closed(self):
            pass

    class Reader:
        async def readline(self):
            request = requests[-1]
            result = (
                {"simulated": simulated}
                if request["method"] == "robot.policies"
                else {"accepted": True}
            )
            return json.dumps({"id": request["id"], "result": result}).encode() + b"\n"

    writer = Writer()

    async def connect(socket):
        assert socket == Path("/owned/duck-a.sock")
        return Reader(), writer

    monkeypatch.setattr(sim_equipment.asyncio, "open_unix_connection", connect)
    if simulated is True:
        await sim_equipment.select_mode(Path("/owned/duck-a.sock"), "roller")
        assert requests[-1]["params"] == {"mode": "roller"}
    else:
        with pytest.raises(RobotRefused, match="explicit simulated"):
            await sim_equipment.select_mode(Path("/owned/duck-a.sock"), "roller")
        assert not any(request["method"] == "robot.setMode" for request in requests)
    assert writer.closed
