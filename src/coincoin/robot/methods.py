"""S1 robot method allowlist shared by the standalone relay and WebRTC.

`tof.stream` is answered by tofd, not robotd: over WebRTC mediad routes it to tofd and its
`tof.frame` notifications come back on the control channel; the TCP relay serves it on its own
tofd-only port instead (relay/duck_relay.py).
"""

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
        "robot.setMode",
        "robot.policies",
        "tof.stream",
    }
)
