# Honest localization and obstacle avoidance for `go_to`

`go_to` ("Va dans ma chambre") used to steer by the simulator's true pose. A real duck does not
have one. By default, navigation now uses only what the robot publishes:

- contact odometry and the IMU in `robot.state` (`odom`, `imu.quat`);
- the head's ToF pose in `robot.state.frames.tof`;
- the 8x8 VL53L5CX depth frames from tofd (`tof.stream` → `tof.frame`: 45° FOV, 4 m, a status
  per zone);
- the known apartment map from `salon_scan/build_navigation.py` (clearance grid and named places).

This note covers the design, the measured results and a verdict for the scanned apartment.

## Pose sources

`Navigator` steers by a `PoseSource` (`src/coincoin/robot/localization.py`):

| `NAV_POSE` | Source | Use |
|---|---|---|
| `estimated` (default) | `EstimatedPose`: MCL from odometry + ToF in the map frame | the honest mode, simulator or robot |
| `oracle` | `OraclePose(SimPose)`: the simulator's true pose | evaluation and comparison only |

In honest mode `SIM_POSE_LINK` is still read, but **only to score** the estimate. The journal and
tests record the error; the controller never sees the true pose. `SimPose` asks the body server for
`oracle.pose` (the sim-realism op) and falls back to `read` (trunk + `imu.quat`) on older
simulators. Honest mode refuses roller (skates) navigation, because there is no contact odometry
while gliding.

A future `maploc` source (upstream microduck#127, `robot.map` with a map-frame pose from SLAM+MCL
inside robotd) only needs to implement the same `PoseSource` protocol: `current()` returning a
`PoseEstimate` (pose, uncertainty, converged/lost), plus an optional relocalization. It also needs
a fixed transform from the maploc frame to the Roborock-derived nav map. That alignment is not
done here, and nothing here depends on maploc.

## Monte-Carlo localization

- **Particles:** 600, in map x, y and yaw.
- **Prediction:** each `robot.state` odometry delta, with a noise model of 8 % of translation,
  5 % of rotation, cross terms and small floors. A gliding (roller) model exists but is unused in
  honest mode.
- **Correction:** each ToF frame. Each zone becomes a beam from the head's ToF frame, levelled
  with the IMU:
  - **valid** (status 5/9): a hit, scored against a two-sided likelihood field of the map
    (σ 6 cm plus a random floor). Hits on the floor or above 1.5 m are ignored;
  - **no target** (255): free space; points sampled along the beam up to 4 m are penalized if they
    fall inside a mapped obstacle below 0.45 m;
  - **other statuses**: ignored.
- **Outlier gating:** hits farther than 12 cm from any mapped surface for most particles are
  treated as unmapped things (furniture, people, a door) and do not weigh the particles, so a box
  does not pull the estimate.
- **Uncertainty:** reported as standard deviations and a map-fit score. Converged means
  σxy < 15 cm and σyaw < 0.15 rad. Lost means σxy > 0.5 m, σyaw > 0.5 rad, or a persistently low
  map fit.

## Start and relocalization

- `NAV_START` gives a known start: a place, `place@heading_deg` or `x,y,heading_deg`. In the
  apartment scene the duck spawns at the dock, `départ@0`. `scripts/live_stack.py --start
  départ@0` sets it.
- With no start, `go_to` first sweeps the head and refuses ("I don't know where I am") unless a
  match is confident.
- **Head sweep:** the duck stands still and looks at −75…+75° in 25° steps. Odometry stitches the
  frames into one composite, which is matched coarse-to-fine around the last estimate (0.6–1.5 m,
  limited heading tolerance). Upstream #127 found that wide composites relocalize while narrow
  wedges do not, and that matches here: a single wedge is never trusted.
- A whole-flat search (`NAV_GLOBAL_RELOCALIZATION=1`) is opt-in. It can be confidently wrong.
- **While walking:** if the estimate stays unconverged for 3 s or is lost, the duck stops and
  sweeps. After two failed relocalizations on a route it gives up and says so: "I lost track of
  where I am on the way to X and stopped (…)".

## Reactive obstacle avoidance

- **Stopping distance:** ToF returns that the map does not explain (more than 15 cm from a mapped
  surface) and that lie on the next stretch of route stop the duck. Two consecutive frames must
  agree. The stretch covers:
  - the duck's radius;
  - the distance walked during the sensor latency (0.3 s × speed);
  - braking (0.1 m);
  - a margin (0.15 m).
- **Bumper:** any return within 6 cm ahead of the body stops the duck, whether mapped or not.
- **Local costmap:** the seen faces are marked with an assumed depth of 0.3 m, then the route is
  replanned with the existing A* (up to four obstacle replans). The localizer's map gets the same
  marks.
- **Verify before giving up:** if no path remains, the duck does not trust the marks at once,
  because they could come from a few centimetres of pose error that make a wall look
  "unexplained". It sweeps the head and relocalizes, then retries from the corrected pose with the
  previous marks. If the way is still blocked, or the pose cannot be confirmed, it stops and says:
  "the way to X is blocked by something that is not on my map (a closed door?); I stopped".

## Measurements

### Offline tests

`tests/unit/test_localization.py` and `tests/unit/test_navigation_honest.py` run deterministically,
with no simulator, on synthetic maps and a ray-cast fake ToF (`tests/fakes/fake_tof.py`). They
cover:

- tracking against drifting odometry;
- kidnapping, which is reported lost;
- relocalization by sweep near a prior, and a single wedge that is not trusted;
- unknown starts, which are refused;
- an unmapped chair, walked around;
- a closed door, which ends in "blocked" without collision;
- roller mode, which is refused;
- the beam and status model and the ToF TCP stream.

In earlier synthetic runs, a whole-flat global relocalization was right in 2 of 3 cases, and in 3
of 3 with a position and heading prior. That is why it is opt-in.

### Live simulator

These runs use the scanned apartment, MuJoCo with the v9 walk policy, robotd, tofd (simulated
VL53L5CX) and the TCP relay. The test is `tests/integration/test_navigation_honest_simulation.py`.
Error is estimate vs `oracle` truth, sampled every 0.25 s.

| Scenario | Result | Time | RMS xy | Max xy | Yaw RMS | Contact |
|---|---|---|---|---|---|---|
| départ → ma chambre | arrived (0.16 m from goal) | 71.9 s | 0.097 m | 0.193 m | 3.1° | 0.25 s |
| → salon | arrived (0.15 m from goal) | 79.4 s | 0.061 m | 0.124 m | 2.5° | 0 |
| Closed door across the bedroom passage | stopped, "blocked … (a closed door?)" after a verify sweep | 68.3 s | 0.102 m | 0.207 m | 3.9° | 0.25 s |
| Unmapped box (25 cm) in the open area west of the start | **failed**: replanned around the box, then refused "blocked" | 44.5 s | 0.156 m | 0.390 m | 3.5° | 0 |

The estimate stayed converged for 95–100 % of samples. Every ToF frame (about 2200 per tour) was
used.

The box failure is the main finding. Between x −2 and −3.5 m, the estimate is biased 0.1–0.25 m
south even without obstacles; the plain tour shows 0.13–0.17 m there too. The likely cause is a
map/world mismatch: the simulated ToF sees the full 3D scan mesh, while the nav map is a 2D slice.
With the duck detouring south of the box, the bias grows, the mapped corridor walls look
"unexplained" from the biased pose, and they get marked as obstacles. The verify sweep converges
to the same biased optimum, so the duck correctly refuses rather than collides, but it does not
reach the room.

Making the "explained" tolerance looser (0.2 m), or dropping marks near walls, was tried. It either
broke the offline door case or let the planner squeeze between a real door and its jambs, so
neither was kept. The live box test is a documented, non-strict `xfail`.

### Over WebRTC, with realism (integration run)

`live_stack.py --link webrtc --places … --start départ@0 --realism real --realism-seed 7`,
ToF over the WebRTC control channel (`TOF_LINK=link`), same apartment, no obstacles:

| Scenario | Result | Time | RMS xy | Max xy | Yaw RMS | Converged |
|---|---|---|---|---|---|---|
| départ → ma chambre | arrived (0.02 m from goal) | 64.9 s | 0.087 m | 0.204 m | 3.2° | 99 % |

991 ToF frames received over WebRTC, all used. The 0.20 m maximum is the first sample: the
true start was 5 cm and −5° off `départ@0`.

Same run with `--obstacles …/examples/obstacles_appartement.json` (a chair in the corridor, the
bedroom door closed, a moving cat):

| Scenario | Result | Time | RMS xy | Max xy | Yaw RMS | Converged |
|---|---|---|---|---|---|---|
| départ → ma chambre, door closed | stopped: "blocked by something that is not on my map (a closed door?)" | 82.5 s | 0.309 m | 0.679 m | 10.7° | 92 % |

It went around the chair (about 25 s stalled beside it) and stopped about 0.6 m in front of the
closed door, without touching it. The error peaks (0.5–0.7 m) came while it was beside the
unmapped chair, because the ToF saw the chair and not the mapped wall. They fell back to 0.02 m
in the bedroom corridor.

## Verdict for this apartment

**Odometry + IMU + one 8x8 ToF on the head is enough for room-to-room navigation from a known start
in the mapped, empty apartment.** It tracks at about 6–10 cm RMS (under 20 cm max), arrives within
about 17 cm of the place, and stops cleanly in front of a closed door.

It is **not enough** in these cases:

- **Unknown start / global relocalization.** An 8x8, 45° sensor sees too little to tell similar
  rooms or corridor sections apart; a whole-flat match can be confidently wrong. Start at the dock
  (or tell it where it is).
- **Unmapped obstacles near walls, and areas where the map disagrees with the 3D world.** A 10–25
  cm pose bias turns walls into false obstacles. Clutter changes with time (chairs, bags), so this
  will happen at home.
- **Long featureless corridors.** Along-axis drift is only bounded when an end wall or doorway is
  in range (4 m). **Glass and mirrors** give false or missing returns. **Low obstacles** under the
  beam (cables, thin chair legs) are invisible at the sensor's 45° cone from head height.
- **Speed.** With policy v9 the duck walks at about 0.12 m/s at the default limits, so a room
  crossing takes 70–80 s. Commands below about 0.2 m/s or 0.8 rad/s do not move it.

**What would help most:**

1. Always start from the dock, or another known pose.
2. Head sweeps at every stop and doorway, not only when lost.
3. A height-aware map built from the same scan the ToF sees, or a map refreshed by maploc.
4. A second, wider or lower sensor: a front ToF or 2D lidar for near-floor obstacles, or a camera
   for place recognition.
5. Upstream maploc, once its frame is aligned to the nav map.

## Transport notes

- **TCP relay:** `relay/duck_relay.py` adds a third listener (`--tof-port`, default 7812) that
  allows only `tof.stream` on tofd's socket and forwards its `tof.frame` notifications. The robot
  listener's allowlist keeps `robot.state`.
- **WebRTC:** mediad's `route.rs` permits `tof.stream` and sends it to tofd's lane. coin-coin
  lists it in `ROBOT_METHODS` (the relay still sends it to tofd, never robotd), and
  `TOF_LINK=link` uses `LinkTofStream`: it subscribes once per control channel and reads the
  `tof.frame` notifications that come back on it. `live_stack.py --link webrtc --places …` sets
  it. Measured on the simulator: about 15 frames/s, all used, no errors (see below).
