"""Coin-coin's robot calls checked against the MicroDuck protocol source, offline.

The protocol is read from a MicroDuck checkout (``MICRODUCK_DIR``, else ``../microduck`` next to
this repository). Upstream ``origin/main`` is preferred, via ``git show`` without touching the
working tree; then ``HEAD``; then the files on disk. Without a checkout every test skips.

What is checked:

* every JSON-RPC method coin-coin sends exists in ``duck-ipc-proto`` (``Call::method``), or is the
  ``mediad`` camera socket method ``media.frame``;
* literal parameter keys match the robot's ``deny_unknown_fields`` structs;
* coin-coin's sound tags are the robot's ``SoundTag`` set;
* methods ``mediad`` refuses over WebRTC are exactly the known, documented set, so a newly
  refused method fails loudly instead of failing on delivery day;
* the protocol API version difference is reported, not hidden.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest

from coincoin.robot.jsonrpc import API_VERSION
from coincoin.robot.methods import ROBOT_METHODS
from coincoin.robot.quack import TAGS

COIN = Path(__file__).resolve().parents[2]
SOURCES = (COIN / "src/coincoin", COIN / "relay")
LIB = "duck-ipc-proto/src/lib.rs"
ROUTE = "mediad/src/route.rs"

# mediad's camera socket, not a Call: it is never routed, so it never reaches a WebRTC peer.
MEDIA_METHODS = frozenset({"media.frame"})

# Coin-coin methods mediad refuses over WebRTC, each with what coin-coin must do instead.
KNOWN_WEBRTC_REFUSALS = {
    "robot.setMode": "mode switch is a decision made in the room; use duckctl/robotctl locally",
}


@dataclass(frozen=True)
class Protocol:
    ref: str
    api_version: int
    methods: dict[str, str]  # method string -> Call variant
    params: dict[str, frozenset[str] | None]  # method -> allowed keys (None: not checkable)
    sound_tags: frozenset[str]
    webrtc: dict[str, bool] | None  # variant -> permitted over WebRTC


def _checkout() -> Path | None:
    configured = os.environ.get("MICRODUCK_DIR")
    if configured:
        candidates = [Path(configured)]
    else:
        candidates = [COIN.parent / "microduck", COIN.parent.parent / "microduck"]
    for candidate in candidates:
        if (candidate / LIB).is_file() or (candidate / ".git").exists():
            return candidate
    return None


def _read(root: Path, path: str) -> tuple[str, str] | None:
    for ref in ("origin/main", "HEAD"):
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "show", f"{ref}:{path}"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            break
        if result.returncode == 0 and result.stdout:
            return ref, result.stdout
    file = root / path
    if file.is_file():
        return "worktree", file.read_text(encoding="utf-8")
    return None


def _strip_comments(source: str) -> str:
    return re.sub(r"//[^\n]*", "", source)


def _block(source: str, opener: str) -> str:
    start = source.index(opener)
    depth = 0
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise ValueError(f"unterminated block: {opener}")


def _struct_fields(source: str, name: str) -> frozenset[str] | None:
    match = re.search(
        rf"#\[serde\(([^)]*)\)\]\s*pub struct {re.escape(name)}\s*\{{([^}}]*)\}}", source
    )
    if match is None or "deny_unknown_fields" not in match.group(1):
        return None
    body = match.group(2)
    if "rename" in body or "flatten" in body:
        return None
    return frozenset(re.findall(r"pub\s+(\w+)\s*:", body))


def parse_protocol(lib: str, route: str | None, ref: str) -> Protocol:
    code = _strip_comments(lib)
    version = re.search(r"pub const API_VERSION: u32 = (\d+);", code)
    assert version is not None, "API_VERSION not found"
    consts = dict(re.findall(r'pub const (\w+): &str = "([^"]+)";', _block(code, "pub mod method")))
    mapping = re.findall(
        r"Call::(\w+)(?:\(_\))?\s*=>\s*method::(\w+)", _block(code, "pub fn method(&self)")
    )
    methods = {consts[const]: variant for variant, const in mapping if const in consts}
    payloads = dict(re.findall(r"\b(\w+)\((?:\w+::)?(\w+)\),", _block(code, "pub enum Call")))
    params: dict[str, frozenset[str] | None] = {}
    for method, variant in methods.items():
        payload = payloads.get(variant)
        params[method] = _struct_fields(code, payload) if payload else frozenset()
    tags_block = _block(code, "pub enum SoundTag")
    tags = frozenset(
        re.sub(r"(?<!^)([A-Z])", r"_\1", word).lower()
        for word in re.findall(r"^\s*([A-Z]\w*),", tags_block, flags=re.MULTILINE)
    )
    webrtc = None
    if route is not None:
        arms = _strip_comments(_block(route, "fn permits"))
        webrtc = {}
        arm = r"((?:[A-Z]\w*(?:\(_\))?\s*\|\s*)*[A-Z]\w*(?:\(_\))?)\s*=>\s*(true|false)"
        for variants, verdict in re.findall(arm, arms):
            for variant in re.findall(r"[A-Z]\w*", variants):
                webrtc[variant] = verdict == "true"
    return Protocol(ref, int(version.group(1)), methods, params, tags, webrtc)


@cache
def protocol() -> Protocol:
    root = _checkout()
    if root is None:
        pytest.skip("no MicroDuck checkout (set MICRODUCK_DIR)")
    lib = _read(root, LIB)
    if lib is None:
        pytest.skip(f"{LIB} not found in {root}")
    route = _read(root, ROUTE)
    return parse_protocol(lib[1], route[1] if route else None, lib[0])


@dataclass(frozen=True)
class Use:
    method: str
    keys: frozenset[str] | None  # literal dict keys, None when not a literal
    where: str


SENDERS = {"call", "notify", "_action_call", "encode_request", "_request"}


def _method_argument(node: ast.Call) -> tuple[str, ast.expr | None] | None:
    strings = [
        (index, arg)
        for index, arg in enumerate(node.args)
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    ]
    for index, arg in strings:
        value = arg.value
        assert isinstance(value, str)
        if re.fullmatch(r"(hello|[a-z_]+\.[A-Za-z_.]+)", value):
            following = node.args[index + 1] if index + 1 < len(node.args) else None
            return value, following
    return None


@cache
def coincoin_uses() -> tuple[Use, ...]:
    uses: list[Use] = []
    for root in SOURCES:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                if name not in SENDERS:
                    continue
                found = _method_argument(node)
                if found is None:
                    continue
                method, params = found
                keys: frozenset[str] | None = None
                if isinstance(params, ast.Dict):
                    literal = [
                        key.value
                        for key in params.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    ]
                    if len(literal) == len(params.keys):
                        keys = frozenset(literal)
                where = f"{path.relative_to(COIN)}:{node.lineno}"
                uses.append(Use(method, keys, where))
    return tuple(uses)


def used_methods() -> set[str]:
    return {use.method for use in coincoin_uses()} | set(ROBOT_METHODS)


def test_scan_finds_the_methods_coincoin_is_known_to_send() -> None:
    methods = {use.method for use in coincoin_uses()}
    assert {"hello", "robot.subscribe", "robot.move", "robot.stop", "media.frame"} <= methods


def test_every_method_coincoin_sends_exists_in_the_protocol() -> None:
    proto = protocol()
    missing = sorted(used_methods() - set(proto.methods) - MEDIA_METHODS)
    assert not missing, f"not in duck-ipc-proto@{proto.ref}: {missing}"


def test_literal_parameters_match_the_robot_structs() -> None:
    proto = protocol()
    problems = []
    for use in coincoin_uses():
        allowed = proto.params.get(use.method)
        if use.keys is None or allowed is None:
            continue
        if extra := use.keys - allowed:
            problems.append(f"{use.where} {use.method} unknown keys {sorted(extra)}")
    assert not problems, f"duck-ipc-proto@{proto.ref} would reject: {problems}"


def test_sound_tags_match_the_robot_voice_bank() -> None:
    proto = protocol()
    assert set(TAGS) == proto.sound_tags, f"duck-ipc-proto@{proto.ref} SoundTag"


def test_webrtc_refusals_are_exactly_the_known_set() -> None:
    proto = protocol()
    if proto.webrtc is None:
        pytest.skip(f"{ROUTE} not available")
    refused = {
        method
        for method in used_methods()
        if method in proto.methods and proto.webrtc.get(proto.methods[method]) is False
    }
    unrouted = {
        method
        for method in used_methods() - MEDIA_METHODS
        if method in proto.methods and proto.methods[method] not in proto.webrtc
    }
    assert not unrouted, f"mediad@{proto.ref} permits() has no arm for {sorted(unrouted)}"
    assert refused == set(KNOWN_WEBRTC_REFUSALS), (
        f"mediad@{proto.ref} refuses over WebRTC: {sorted(refused)}; "
        f"known: {sorted(KNOWN_WEBRTC_REFUSALS)}"
    )


def test_api_version_gap_is_reported(record_property: Callable[[str, object], None]) -> None:
    proto = protocol()
    record_property("coincoin_api_version", API_VERSION)
    record_property("robot_api_version", proto.api_version)
    # Additive versions are accepted by robotd's hello; a client older than the robot still
    # works. A client newer than the robot it talks to is the real failure.
    assert proto.api_version >= API_VERSION, (
        f"coin-coin speaks v{API_VERSION}, duck-ipc-proto@{proto.ref} is v{proto.api_version}"
    )


def test_removed_methods_are_not_used() -> None:
    proto = protocol()
    # Removed in API v50: a client still asking gets METHOD_NOT_FOUND from a released robot.
    removed = {"robot.remoteSessionActive"} - set(proto.methods)
    assert not used_methods() & removed


LIB_FIXTURE = """
pub const API_VERSION: u32 = 51;
pub mod method {
    pub const HELLO: &str = "hello";
    pub const ROBOT_MOVE: &str = "robot.move";
    pub const ROBOT_SET_MODE: &str = "robot.setMode";
    pub const ROBOT_STOP: &str = "robot.stop";
}
pub enum Call {
    Hello(HelloParams),
    RobotMove(MoveParams),
    RobotSetMode(SetModeParams),
    RobotStop,
}
impl Call {
    pub fn method(&self) -> &'static str {
        match self {
            Call::Hello(_) => method::HELLO,
            Call::RobotMove(_) => method::ROBOT_MOVE, // comment
            Call::RobotSetMode(_) => method::ROBOT_SET_MODE,
            Call::RobotStop => method::ROBOT_STOP,
        }
    }
}
#[serde(default, deny_unknown_fields)]
pub struct MoveParams {
    pub vx: f64,
    pub vy: f64,
    pub vyaw: f64,
}
#[serde(default)]
pub struct SetModeParams {
    pub mode: String,
}
pub enum SoundTag {
    Alarm,
    Wheee,
}
"""

ROUTE_FIXTURE = """
fn permits(call: &proto::Call) -> bool {
    match call {
        Hello(_) => true,
        // RobotStop => false,
        RobotMove(_) | RobotStop => true,
        RobotSetMode(_) => false,
    }
}
"""


def test_parser_on_a_fixed_fixture() -> None:
    proto = parse_protocol(LIB_FIXTURE, ROUTE_FIXTURE, "fixture")
    assert proto.api_version == 51
    assert proto.methods == {
        "hello": "Hello",
        "robot.move": "RobotMove",
        "robot.setMode": "RobotSetMode",
        "robot.stop": "RobotStop",
    }
    assert proto.params["robot.move"] == {"vx", "vy", "vyaw"}
    assert proto.params["robot.setMode"] is None
    assert proto.params["robot.stop"] == frozenset()
    assert proto.sound_tags == {"alarm", "wheee"}
    assert proto.webrtc == {
        "Hello": True,
        "RobotMove": True,
        "RobotStop": True,
        "RobotSetMode": False,
    }
