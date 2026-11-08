# Honest localization and obstacle avoidance

`go_to` uses contact odometry, the IMU and head-mounted 8x8 ToF observations instead of the
simulator's exact pose. Supply your own navigation map with `--places /path/to/navigation.json`
and a known starting pose with `--start dock@0`. Private scans, layouts, scene-specific
coordinates and session traces are not distributed.

## Pose sources and map format

`NAV_POSE=estimated` is the default, using `EstimatedPose` (Monte-Carlo localization).
`NAV_POSE=oracle` explicitly opts into simulation truth for diagnostic comparison.
In estimated mode `SIM_POSE_LINK`, when available, is used only for scoring, never control.
Honest room navigation refuses roller mode because contact odometry is unavailable while gliding;
manual roller movements and equipment changes remain available.

The navigation JSON contains `width`, `height`, `origin_xy_m`, `resolution_m`,
`robot_radius_m`, `clearance_cm_uint8_base64` and `places` (named `{x, y}` coordinates).
The decoded clearance grid has one byte per cell, expressing clearance in centimetres.
The scene and map must use the same world frame. A future robotd `maploc` pose source also
requires an explicit transform into this map frame; that integration is not complete.

## Localization and recovery

- 600 particles predict from every odometry delta, including manual move and turn commands.
- ToF frames correct the particles using a likelihood field of mapped surfaces.
  Valid zones (status 5/9) contribute hits, status 255 contributes free space, and other
  statuses are ignored. Mapped-surface outliers are gated to reduce disturbance from new objects.
- Floor returns are excluded both by height and by a per-frame sampled-consensus plane fit
  (`FloorTracker`, within 2.5 m, at most 12 degrees of tilt and 6 cm level offset).
  This prevents IMU-leveling error and gait sway from lifting floor returns into apparent obstacles.
- Uncertainty includes position and heading standard deviations and a map-fit score.
  Convergence requires position spread below 15 cm and heading spread below 0.15 rad.
  Persistent poor fit or spreads above 0.5 m / 0.5 rad trigger lost handling.
- `NAV_START` accepts a place, `place@heading_deg` or `x,y,heading_deg`.
  Unknown starts are refused unless a sweep produces a confident match.
  Whole-map search (`NAV_GLOBAL_RELOCALIZATION=1`) is opt-in and can be confidently wrong.
- Before `go_to` after manual motion, or after sustained uncertainty, the duck stops and sweeps
  its head from -75 to +75 degrees in 25-degree steps. Odometry stitches a composite scan.
  The local search window grows with travel and rotation since the last confirmed fix.
- Large relocalization jumps are accepted only when they fit substantially better than nearby
  matches. Two failed relocalizations on a route stop navigation with an explicit lost message.

## Obstacle avoidance and trip outcomes

Two consecutive ToF frames must agree on an unexplained obstacle along the next route segment.
The stopping horizon covers body radius, sensor latency, braking distance and a margin.
A near-body bumper check applies even to mapped obstacles. Observed faces are expanded by an
assumed 0.3 m depth and marked in a local costmap for A* replanning.

Before declaring an unexpected blockage, navigation sweeps and relocalizes to check whether a
mapped wall was seen from a wrong pose. An unconfirmed pose reports **lost**, not **blocked**.
Motion is stopped in either case. `status` exposes the last trip's target, progress and outcome.
Talking over a trip does not discard its result; late outcomes are announced to the conversation.
Explicit stop/cancellation remains available.

## Transport and simulation launch

Over WebRTC, `TOF_LINK=link` subscribes to `tof.stream` on the robot control datachannel.
The development relay also exposes a dedicated ToF listener. The launcher sets the appropriate
source and forces simulation connections to loopback, ignoring a hardware delivery profile.

```sh
uv run --no-sync python scripts/live_stack.py --link webrtc --scene /path/to/scene.xml \
  --places /path/to/navigation.json --start dock@0 --realism real
```

For convenient simulation-only voice requests to fit/remove skates, add `--sim-auto-equipment`.
It uses this stack's local robotd Unix socket, confirms both peers explicitly report simulation,
and waits for mode confirmation, homing and safe state before unlocking motion.
WebRTC still refuses remote `robot.setMode`; the hardware flow still requires a person.
The option is incompatible with `--no-assist`.

## Reproducible evaluation

Offline localization and navigation tests use synthetic maps and a ray-cast fake ToF.
They cover drifting odometry, kidnapping, sweep recovery, unknown starts, unexpected obstacles,
closed doors, roller refusal, beam/status parsing and transport.

The headless trace harness takes user-supplied scene and map files:

```sh
flock -w 1800 /tmp/microduck-sim-agents.lock \
  .venv/bin/python -m tests.integration.nav_repro \
  --scene /path/to/scene.xml --map /path/to/navigation.json --start dock@0 \
  --realism real --seed 0 --prelude --out logs/nav_repro/run.jsonl room-a room-b dock
```

Truth is read only beside the controller to measure error. Inspect final distance, RMS/max
position and heading errors, contact duration, refusal reasons and false arrivals.
Hold the shared simulator lock and never interfere with another user's running stack.

## Remaining limitations

This is not general-purpose hardware-validated navigation. An 8x8 narrow-field sensor cannot
disambiguate featureless spaces or similar map regions reliably. A high map-fit score can still
correspond to the wrong location: **false arrival reports remain possible** in ambiguous areas.
Map/scene disagreement and new obstacles near walls can create localization bias and false
blockages. Thin low objects, dark surfaces, glass and mirrors need additional sensing or testing.

Improve ambiguity-aware arrival checks, observations at doorways/stops, camera-based place
recognition, map consistency and temporary obstacle expiry before claiming robust autonomy.
A known starting pose is supplied information, not proof of global relocalization.

An in-place counterclockwise turning stall was observed with the ideal (`off`) simulation profile;
turning clockwise or with forward movement behaved differently. Its policy/simulator cause is
not resolved. An existing WebRTC reconnect regression also failed during validation
(`test_mediad_restart_stops_motion_and_reconnects`, subscription readiness and late data after EOF).
Do not interpret the new simulator features as evidence that these failures are fixed.
