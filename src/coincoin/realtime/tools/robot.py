"""Model-facing robot contracts; every action is delegated to the governor."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from coincoin.robot.governor import LOOK_POINTS

if TYPE_CHECKING:
    from coincoin.app import App
    from coincoin.robot.capabilities import Capabilities

RobotHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _number(low: float, high: float) -> dict[str, Any]:
    return {"type": "number", "minimum": low, "maximum": high}


def schemas(
    max_vx: float,
    max_vy: float,
    max_vyaw: float,
    max_move_s: float,
    places: list[str] | None = None,
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
        "set_mode": {"mode": {"type": "string", "enum": ["walk", "roller"]}},
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
    if places:
        properties["go_to"] = {
            "place": {"type": "string", "enum": list(places)},
            "mode": {"type": "string", "enum": ["walk", "roller"]},
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
    if "go_to" in result:
        result["go_to"]["required"] = ["place"]
    return result


DESCRIPTIONS = {
    "move": (
        "Move for duration_s seconds, then stop. Velocities: vx m/s, positive = forward "
        "(avancer), negative = backward (reculer); vy m/s, positive = left; vyaw rad/s, "
        "positive = turn left. At least one velocity must be non-zero; to go forward use "
        "vx about 0.2. Returns once the movement has finished "
        "or was interrupted. Robot actions run one at a time in call order, so for a "
        "multi-step request call each step in order (e.g. move, turn, move, sit)."
    ),
    "turn": (
        "Turn degrees, positive = left; a left quarter-turn is +90. "
        "Returns once the turn has finished."
    ),
    "stop": "Stop all robot motion immediately and cancel any pending steps.",
    "set_mode": (
        "Switch locomotion: roller = skate/glide on skates (patiner, glisser, mettre les patins); "
        "walk = walk on feet (marcher, retirer les patins, revenir a la marche). "
        "This stops motion and waits for the mode change. It does not move: "
        "call move afterwards if requested. The mode persists until changed again. On a real "
        "robot a person must fit or remove skates; the robot may require local mode selection."
    ),
    "look": (
        "Look: provide either direction alone or all of x, y, z, never both. "
        "Coordinates are x forward, y left, z up (metres, body frame). hold_s is optional."
    ),
    "set_pose": "Set a persistent bounded pose, z metres, roll/pitch radians.",
    "reset_pose": "Reset the persistent pose.",
    "sit": "Sit, idempotent.",
    "stand": "Stand, idempotent.",
    "do_skill": "Perform one currently available skill.",
    "quack": "Quack; wheee can be held for at most 3 seconds.",
    "status": "Short robot posture, safety, mode and health summary.",
    "go_to": (
        "Walk or skate by itself to a named room of the apartment, avoiding walls and furniture, "
        "then stop there (aller dans ma chambre, aller au salon). To come back (reviens, "
        "retourne, rentre) use place départ, the starting point. mode is optional: roller = on "
        "skates, faster (en patins, en patinant); walk = on feet (à pied, sans patins, en "
        "marchant); omit it to keep the current mode. Returns once arrived, which can take a "
        "minute or more; stop interrupts it."
    ),
}


PERSON_DESCRIPTIONS = {
    "set_mode": (
        "Change locomotion: roller = skates (patiner, glisser, mettre les patins); walk = feet "
        "(marcher, retirer les patins). THIS ROBOT CANNOT CHANGE MODE BY ITSELF over this link: "
        "calling set_mode asks a person to fit or remove the skates and select the mode on the "
        "robot, then waits for the robot to report it. Tell the user plainly that a person has "
        "to do it. Motion is disabled until the mode changes; you are told when it does."
    ),
    "stand": (
        "Stand, idempotent. If the duck has fallen over, it cannot get up by itself: ask a "
        "person to put it back on its feet."
    ),
}

SUFFIX = (
    " Briefly announce the action in the conversation language. "
    "If clamped is present, explain the limited values, including duration."
)


def tool_description(name: str, capabilities: Capabilities | None = None) -> str:
    """Model-facing description, adapted to what the connected robot actually allows."""
    needs_person = capabilities is not None and (
        (name == "set_mode" and capabilities.mode_switch_needs_person)
        or (
            name == "stand"
            and (not capabilities.simulated or capabilities.mode_switch_needs_person)
        )
    )
    return (PERSON_DESCRIPTIONS[name] if needs_person else DESCRIPTIONS[name]) + SUFFIX


def tool_availability(name: str, capabilities: Capabilities | None) -> str | None:
    """Why a tool is hidden for this robot, or None when it may be offered."""
    if name == "go_to" and (capabilities is None or not capabilities.simulated):
        return "room navigation uses the simulator's true pose; this robot is not simulated"
    return None


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


def install_tools(
    app: App, handler: RobotHandler, places: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    settings = app.settings
    contracts = schemas(
        settings.max_vx, settings.max_vy, settings.max_vyaw, settings.max_move_s, places
    )
    for name, schema in contracts.items():

        async def dispatch(
            _app: App, arguments: dict[str, Any], *, tool_name: str = name
        ) -> dict[str, Any]:
            return await handler(tool_name, arguments)

        app.register_tool(
            name,
            tool_description(name),
            schema,
            dispatch,
            manages_timeout=name not in {"stop", "status", "quack"},
        )
    return contracts
