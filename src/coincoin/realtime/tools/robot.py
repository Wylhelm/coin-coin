"""Model-facing robot contracts; every action is delegated to the governor."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from coincoin.robot.governor import LOOK_POINTS

if TYPE_CHECKING:
    from coincoin.app import App

RobotHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _number(low: float, high: float) -> dict[str, Any]:
    return {"type": "number", "minimum": low, "maximum": high}


def schemas(
    max_vx: float, max_vy: float, max_vyaw: float, max_move_s: float
) -> dict[str, dict[str, Any]]:
    properties: dict[str, dict[str, Any]] = {
        "move": {
            "vx": _number(-max_vx, max_vx),
            "vy": _number(-max_vy, max_vy),
            "vyaw": _number(-max_vyaw, max_vyaw),
            "duration_s": {"type": "number", "exclusiveMinimum": 0, "maximum": max_move_s},
        },
        "turn": {"degrees": _number(-360, 360)},
        "stop": {},
        "look": {
            "direction": {"type": "string", "enum": list(LOOK_POINTS)},
            "x": {"type": "number"},
            "y": {"type": "number"},
            "z": {"type": "number"},
            "hold_s": {**_number(0, 10), "default": 3},
        },
        "set_pose": {
            "z": _number(-0.025, 0.010),
            "roll": _number(-0.26, 0.26),
            "pitch": _number(-0.26, 0.26),
        },
        "reset_pose": {},
        "sit": {},
        "stand": {},
        "do_skill": {"name": {"type": "string", "enum": []}},
        "quack": {
            "tag": {
                "type": "string",
                "enum": ["greet", "inquire", "chirp", "coo", "alarm", "peck", "wheee"],
            },
            "hold_s": {**_number(0, 3), "default": 0},
        },
        "status": {},
    }
    result = {
        name: {
            "type": "object",
            "properties": fields,
            "required": list(fields) if name not in {"look", "quack"} else [],
            "additionalProperties": False,
        }
        for name, fields in properties.items()
    }
    result["quack"]["required"] = ["tag"]
    result["look"]["oneOf"] = [
        {"required": ["direction"], "not": {"anyOf": [{"required": [k]} for k in ("x", "y", "z")]}},
        {"required": ["x", "y", "z"], "not": {"required": ["direction"]}},
    ]
    return result


DESCRIPTIONS = {
    "move": (
        "Avance / move: returns immediately; movement lasts duration_s seconds, then stops. "
        "A new move or turn replaces the previous motion; do not chain them before it finishes."
    ),
    "turn": "Tourne / turn degrees, positive = left; tourne a gauche = +90. Returns immediately.",
    "stop": "Arrete / stop all robot motion immediately.",
    "look": "Regarde / look in a direction or at x forward, y left, z up (metres, body frame).",
    "set_pose": "Posture / set a persistent bounded pose, z metres, roll/pitch radians.",
    "reset_pose": "Posture neutre / reset the persistent pose.",
    "sit": "Assieds-toi / sit, idempotent.",
    "stand": "Leve-toi / stand, idempotent.",
    "do_skill": "Figure / perform one currently available skill.",
    "quack": "Cancane / quack; S5 logs only, no audible sound until S6.",
    "status": "Etat / short robot posture, safety, mode and health summary.",
}


def invalid_arguments(name: str, arguments: dict[str, Any], schema: dict[str, Any]) -> str | None:
    fields = schema["properties"]
    if extra := arguments.keys() - fields.keys():
        return f"Unknown arguments: {', '.join(sorted(extra))}"
    if missing := set(schema["required"]) - arguments.keys():
        return f"Missing arguments: {', '.join(sorted(missing))}"
    for key, value in arguments.items():
        contract = fields[key]
        if contract["type"] == "number":
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                return f"{key} must be a finite number"
        elif not isinstance(value, str):
            return f"{key} must be a string"
        if "enum" in contract and name != "do_skill" and value not in contract["enum"]:
            return f"{key} must be one of: {', '.join(contract['enum'])}"
    if name == "look":
        direction = "direction" in arguments
        point = any(k in arguments for k in ("x", "y", "z"))
        if direction == point or (point and not all(k in arguments for k in ("x", "y", "z"))):
            return "Use one direction or a complete x, y, z point"
    if name == "quack":
        hold = arguments.get("hold_s", 0)
        if not 0 <= hold <= 3 or (hold and arguments["tag"] != "wheee"):
            return "Only wheee accepts a hold, between 0 and 3 seconds"
    return None


def install_tools(app: App, handler: RobotHandler) -> dict[str, dict[str, Any]]:
    settings = app.settings
    contracts = schemas(settings.max_vx, settings.max_vy, settings.max_vyaw, settings.max_move_s)
    for name, schema in contracts.items():

        async def dispatch(
            _app: App, arguments: dict[str, Any], *, tool_name: str = name
        ) -> dict[str, Any]:
            return await handler(tool_name, arguments)

        app.register_tool(
            name,
            DESCRIPTIONS[name] + " Announce briefly what you do / annonce brievement l'action. "
            "If clamped is present, say you go slower / dis que tu vas moins vite.",
            schema,
            dispatch,
        )
    return contracts
