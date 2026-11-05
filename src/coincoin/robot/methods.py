"""S1 robot method allowlist shared by the standalone relay and WebRTC."""

ROBOT_METHODS = frozenset(
    {
        "hello",
        "robot.move",
        "robot.stop",
        "robot.head",
        "robot.look",
        "robot.pose",
        "robot.mouth",
        "robot.sound",
        "robot.do",
        "robot.enable",
        "robot.subscribe",
        "robot.health",
        "robot.skills",
        "robot.mode",
        "robot.policies",
    }
)
