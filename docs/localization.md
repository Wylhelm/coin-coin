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
simulators. On skates the wheels carry no contact odometry, so honest mode measures the glide from
consecutive ToF frames (*Glide odometry* below) and only accepts roller navigation while that
measurement holds.

A future `maploc` source (upstream microduck#127, `robot.map` with a map-frame pose from SLAM+MCL
inside robotd) only needs to implement the same `PoseSource` protocol: `current()` returning a
`PoseEstimate` (pose, uncertainty, converged/lost), plus an optional relocalization. It also needs
a fixed transform from the maploc frame to the Roborock-derived nav map. That alignment is not
done here, and nothing here depends on maploc.

## Monte-Carlo localization

- **Particles:** 600, in map x, y and yaw.
- **Prediction:** each `robot.state` odometry delta, with a noise model of 8 % of translation,
  5 % of rotation, cross terms and small floors. In roller mode that delta is ignored: the
  particles move by the glide measured from the last two frames, with that match's own σ per axis,
  and diffuse along an axis the view could not pin (*Glide odometry*).
- **Correction:** each ToF frame. Each zone becomes a beam from the head's ToF frame, levelled
  with the IMU:
  - **valid** (status 5/9): a hit, scored against a two-sided likelihood field of the map
    (σ 6 cm plus a random floor). Hits on the floor or above 1.5 m are ignored. "On the floor"
    means below 5 cm in the level frame **or** on the floor plane fitted to that frame's own
    returns (`FloorTracker`, a sampled-consensus fit within 2.5 m, at most 20° and 6 cm off
    level; fits above 12° must explain at least 40% of the valid returns): level-frame error lifts
    floor returns 1–2 m ahead to 0.05–0.3 m,
    where they would otherwise look like obstacles. Only the plane's inliers are reclassified;
    other returns keep their height;
  - **no target** (255): a missing return, not proof that the floor map is empty;
  - **other statuses**: ignored.
- **Free rays:** samples from rays, including samples before valid hits, are retained for
  diagnostics but do not score poses in tracking or head-sweep matching. The 2D map has no
  obstacle heights, so a beam can pass over a mapped footprint; dark or transparent surfaces
  can also produce a missing return.
- **Outlier gating:** during confirmed walking, hits more than 12 cm from a mapped surface at the
  predicted pose do not weigh the particles. If fewer than six hits remain (or fewer than all hits
  in a smaller frame), that frame does not correct the pose; it is not scored using all the hits
  as a fallback. An individual correction that moves the estimate too far is rejected, and
  repeated rejections invalidate confirmation.
- **Nearby unmapped objects:** two frames containing at least three unexplained returns in a
  1.5 m corridor ahead suspend corrections and make the pose unconfirmed while the obstacle is in
  view. Corrections resume only after sustained map agreement. If the disagreement lasts eight
  seconds, confirmation remains invalid until a new explicit fix. A low map fit by itself does
  not trigger this object-specific suspension.
- **Dispersion and fit:** the particle standard deviations and a map-fit score are not bounds on
  the actual error. A tight, good-fitting cluster can still be wrong. `converged` also requires a
  known origin or accepted sweep, adequate fit, and a depth frame within 0.5 s on skates. The lost
  flag means σxy > 0.5 m, σyaw > 0.5 rad, or low map fit; navigation permits a short persistence
  before stopping for non-convergence.

### Glide odometry (skates)

`scan_from` already removes the head pose and the IMU tilt, so two consecutive frames' returns
differ by the body's own rigid motion — the glide the passive wheels cannot report. `match_glide`
recovers it from the ToF stream alone, with nothing the real robot lacks: nearest-neighbour ICP,
4 rounds, seeded with the IMU's heading, on the 2D level-frame returns (64 points at most, so the
cost is small).

The match is refused, and the filter diffuses instead — a wrong step is worse than no step — when:

- either frame has fewer than 8 returns, or fewer than half the previous frame's returns keep a
  correspondence within 12 cm;
- the frames are closer than 5 ms or further apart than 150 ms (a 15 Hz stream; a longer gap
  means the body may have moved unmeasured);
- the 2D RMS residual exceeds 5 cm;
- the fit's turn disagrees with the IMU by more than 0.25 rad;
- the step is faster than the skates can go (0.6 m/s, plus an 8 cm margin).

A view only measures the glide it can tell apart from standing still: a wall the robot faces pins
the step towards it and says nothing about sliding along it, however good the fit looks. The match
therefore reports a σ per body axis, from a point-to-plane information matrix (Schur-complement
marginal precision, inflated 1.5×, floored at 0.8 cm and never below the residual). An axis whose
σ exceeds 2 cm comes back unmeasured and the filter diffuses along it; if both axes are
unmeasured the whole step is refused. Returns closer than 2 cm are one surface sample — the zone
grid stacks the rows of a column onto the same 2D point — and are deduplicated before the normals
are computed.

`EstimatedPose.glide_odometry` says whether the stream is currently measuring: the last match
succeeded within the last 10 frames and a frame arrived within 0.5 s. On the synthetic flat, a
192-pose grid with a 2 cm glide measures 74 % of poses, with a median forward error of 0.2 cm and
a 90th percentile of 0.5 cm; the rest diffuse. `go_to` on skates is refused with an explanation
when `glide_odometry` is false, and the mode is never switched automatically.

## Start and relocalization

- `NAV_START` gives a known start: a place, `place@heading_deg` or `x,y,heading_deg`. In the
  apartment scene the duck spawns at the dock, `départ@0`. `scripts/live_stack.py --start
  départ@0` sets it, and `coincoin-appartement.sh` passes `--start 'départ@0'` by default (a
  later `--start`, even empty, wins).
- The start is the pose at the **odometry origin** (sim spawn or controller start), not the pose
  when Coin-Coin connects. The duck turns and slides while it stands up (about −57° and 0.2 m on
  realism seed 1); resetting to the start at connection time ignored that motion, so the filter
  stayed "converged" while wrong and sent the duck to the wrong room. The estimate is now the
  start composed with the accumulated odometry, with σ widened by the odometry already
  travelled. Caveat: restarting Coin-Coin over a running sim dead-reckons from the spawn.
- `status` reports an approximate distance to a named landmark, not an asserted room, or says
  that the position is uncertain and a head sweep is needed.
- Entering or leaving roller mode, a detected pickup, or an equipment change in progress
  invalidates the previous confirmation. Returning to walk requires a fresh sweep; it does not
  reset the duck to the dock.
- A relocalization match that wanders off the prior also clears the confirmed flag, so the
  rejection persists until a new confident match.
- A proposed sweep match is evaluated separately before replacing the particle filter. After a
  nearby obstacle is observed, its correction must remain within 0.3 m / 0.2 rad of the
  pre-obstacle pose, carried forward by odometry, and must not cross observed obstacle cells.
  Unmapped returns at that prior are excluded from the sweep match, including returns above the
  robot's body: otherwise a box or door can be mistaken for a wall. The marks stay available for
  the next localization attempt until a match is accepted. This is conservative: if the tracked
  prior itself is badly wrong, the duck may have to refuse rather than guess.
- With no start, `go_to` first sweeps the head and refuses ("I don't know where I am") unless a
  match is confident.
- **Head sweep:** the duck stands still and looks at −75…+75° in 25° steps. Odometry stitches the
  frames into one composite, which is matched coarse-to-fine around the last estimate (0.6–1.5 m,
  limited heading tolerance). Frames are collected and odometry is propagated during the sweep,
  but individual frames do not correct the tracked pose while the head is moving. Upstream #127
  found that wide composites relocalize while narrow wedges do not, and that matches here: a
  single wedge is never trusted.
- The filter tracks during every motion (manual `move`/`turn` too), but a manual motion is not
  checked against a goal, so the next `go_to` first sweeps and relocalizes. The search window
  also grows with the odometry travelled and turned since the last confirmed fix (10 %, sway
  under 0.2 rad ignored), because the spread alone can be overconfident.
- A match that moves the pose more than 0.3 m or 0.2 rad from the tracked one is only taken if
  it fits the sweep at least 10 points better than the best match near the tracked pose: in the
  wide rooms, where the ToF reaches few walls, a look-alike a metre off can score as well.
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
- **Bumper:** a mapped return close ahead during forward motion triggers a stop and replan or a
  localization check; unexplained returns are handled by the two-frame obstacle check.
- **Local costmap:** the seen faces are marked with an assumed depth of 0.3 m, then the route is
  replanned with the existing A* (up to four obstacle replans). These temporary marks are not
  added to the localizer's static map: otherwise a false obstacle becomes a false landmark.
- **Corners:** a waypoint is skipped only when the direct segment to the next one remains clear
  in the planning map, including temporary obstacles. Proximity alone cut a mapped corner and
  produced a brief wall contact in an isolated simulator run. The current segment is also
  rechecked while steering; when the pose leaves it and the new segment is unsafe, the robot
  stops before replanning.
- **Arrival:** walking targets a 0.10 m radius around the named place rather than 0.20 m, leaving
  some margin for localization error and stopping. This is not a guarantee of actual arrival.
- **Verify before giving up:** if no path remains, the duck does not trust the marks at once,
  because they could come from a few centimetres of pose error that make a wall look
  "unexplained". It sweeps the head and relocalizes, then retries from the corrected pose with the
  previous marks. If the way is still blocked from a confirmed pose, it stops and says: "the way
  to X may be blocked by something that is not on my map; I stopped and cannot confirm my
  position". A failed sweep likewise reports the uncertainty rather than asserting a closed door.
  After a blocked stop, subsequent matching frames alone cannot restore confirmation.
- **Status:** `status` reports the last trip: "navigation: going to X", "arrived at X",
  "stopped before reaching X: reason" or "did not go to X: reason". Starting manual motion,
  changing locomotion mode or detecting pickup clears a stale last-arrival report.
- **Conversation:** a `go_to` keeps walking when the user talks over it. Its result can no longer
  answer the cancelled tool call, so it is announced to the conversation when the trip ends
  (journal `tool_result_late`).
- **After a stop and sweep:** the previous forward command and sweep's last scan are not reused as
  evidence of a new collision. Fresh frames remain subject to the normal obstacle checks.

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
- roller mode: a trip on skates is accepted while the ToF measures the glide, and refused with an
  explanation otherwise; the mode is never switched automatically (the user is offered
  `set_mode walk` first);
- the beam and status model and the ToF TCP stream.

In earlier synthetic runs, a whole-flat global relocalization was right in 2 of 3 cases, and in 3
of 3 with a position and heading prior. That is why it is opt-in.

### Live simulator (historical, before the latest localization changes)

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

### Over WebRTC, with realism (historical integration run)

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
closed door. This WebRTC trace did not measure contacts, so it cannot establish that no contact
occurred. The error peaks (0.5–0.7 m) came while it was beside the unmapped chair, because the
ToF saw the chair and not the mapped wall. They fell back to 0.02 m in the bedroom corridor.

## The "blocked" incident (2026-11-08)

With `--realism real`, after "move 5 s, turn 180", `go_to salon` and `go_to ma chambre` ended
with "blocked by something not on my map", and "Reviens au départ" was refused as lost while the
filter claimed 3 cm / 1°. The scene has no obstacles. Measured with
`tests/integration/nav_repro.py`, which logs the estimate against the simulator truth; the truth
is used only for scoring.

Root causes:

- **Floor returns read as obstacles.** The level frame has a pitch error of about +2° (off) or
  +3.7° (real), plus ±3° of gait sway. At 1–2 m this lifts floor hits above the floor cut. At the
  true pose, 12% (off) and 26% (real) of low hits matched nothing on the map. The fake obstacles
  closed the corridor ("blocked") and pulled the heading: the estimate drifted to 138° while
  odometry said 179°. Fix: each frame fits the floor plane (MSAC, ≤ 2.5 m, ≤ 12° tilt) and
  treats its inliers as floor. Unexplained low hits drop to about 4% (real) and 1% (off).
- **Odometry yaw is fine.** It is within about 1° of the truth once the robot has stopped
  turning. The "30% under" figure was read while the robot was still turning. The realism IMU
  model matches the LSM6DSV16X datasheet, so microduck_rl is unchanged.
- **Overconfident filter.** MCL did run during manual motions, but the false floor hits were
  consistent with each other, so the filter converged on them. In addition, relocalization
  searched around the wrong prior, and summed gait sway inflated "rotation since fix". Fixes:
  - before a go_to that follows a manual move or turn (or when the pose is not converged), the
    duck does a head sweep;
  - the relocalization window grows with the travel and the net rotation (steps ≥ 0.2 rad) since
    the last fix;
  - a relocalization that jumps more than 0.3 m or 0.2 rad from the tracked pose must fit the
    sweep better than the best nearby match, by a margin of 0.1;
  - after a stall, a relocalization that does not converge reports "lost", not "blocked".
- **Outcome never told.** When the user spoke during the trip, barge-in cancelled the go_to tool
  task while the walk went on, so its result was never sent to the conversation. Motion tool
  tasks now survive barge-in, and a result that arrives late is announced. `status` now includes
  the navigation state (going to / arrived at / stopped before reaching … : reason).

Results with `--realism real` and the prelude ("move 5 s, turn 180"), then salon → ma chambre →
départ. Error is RMS / max, in metres.

| seed | salon | ma chambre | départ |
|---|---|---|---|
| 0 | OK 0.10 / 0.15 | OK 0.11 / 0.19 | OK 0.16 / 0.33 |
| 1 | OK 0.13 / 0.19 | OK 0.12 / 0.24 | "arrived", but 0.70 m from the place (0.52 / 1.02) |
| 2 | OK 0.06 / 0.09 | OK 0.10 / 0.23 | refused: lost (0.28 / 0.62) |

Direct from the start, real (seed 3): salon OK (0.20 / 0.43), ma chambre OK (0.11 / 0.29).

Both départ failures come from the wide open area at x −4.5…−2. The 8x8 ToF sees almost nothing
there, so lateral drift is not corrected. In seed 1 a relocalization there matched a pose 0.4 m
to the side at fit 0.92: the map cannot tell the two poses apart. Seed 2 honestly reported
"lost".

With `--realism off`, the ideal twin stalls on CCW in-place turns. At vyaw 1.0–2.0 it rotates
about 8° and stops, while CW in-place turns and CCW turns with forward speed work. The same
happens at the previous commit, so it is a policy/sim behaviour, not a localization one. As a
result, "turn 180" times out and `go_to salon` (which starts with a large CCW turn) ends "stuck";
ma chambre and départ succeed.

## Follow-up: starting pose and corridor drift

The spawn is the odometry origin, not the pose after standing. An initial fix that accounted for
the odometry already accumulated at connection improved several trips, but a later realistic
run still drifted by more than a metre while the particles reported only a few centimetres of
spread. Two additional causes were established:

- Free-ray penalties treated missing ToF returns and a 3D ray above a 2D footprint as proof
  against the map. On the first 38 seconds of that recorded path, replaying the same sensor data
  gave 0.279 m RMS / 0.432 m maximum position error with the old model and 0.066 m / 0.102 m
  with positive returns only. This offline comparison is not a closed-loop navigation result.
- Temporary obstacle marks were being added to the localization map and could reinforce an
  incorrect pose. They now affect only route planning.

An intermediate closed-loop run still stopped on the way to the salon even though the recorded
position error there was about 0.08 m. After stopping and sweeping, the controller reused the
old forward-command flag and sweep scan, immediately triggering the same bumper condition
again. It now resets that state and waits for fresh observations.

Another seed 0 run after a manual move and mode changes falsely reported a blocked salon and
later got stuck returning to the dock. Some recorded walking frames show a floor slope of roughly
12–17° in the computed level frame, outside the previous 12° floor-fit limit; many floor returns
were then treated as obstacles. The floor model now admits up to 20° only with strong support,
while retaining its near-origin height, span and inlier checks. Simply raising the limit without
the support check made a synthetic chair scenario cross a wall.

With the current steering and floor model, isolated TCP runs reached the bedroom and salon
0.114 m and 0.143 m from their targets, and detoured around an unmapped 25 cm box to within
0.121 m. No contact with world-box geometry was detected in those runs. A closed-door run stopped
without contact and left the position unconfirmed (the final estimate was 0.082 m from truth).
An earlier closed-door run had falsely confirmed a pose 0.872 m away; the blocked-sweep checks
above were added in response. These are individual runs, not guarantees.

In a separate WebRTC run before the floor-tilt change, realism seed 1 switched to skates, refused
a skating trip, switched back to walking and made four trips. Distances are measured after
stopping against simulator truth, which is not used to control the robot:

| Trip | Final distance to target | Position RMS / maximum |
|---|---:|---:|
| départ → ma chambre | 0.090 m | 0.068 / 0.129 m |
| → salon | 0.195 m | 0.072 / 0.141 m |
| → ma chambre | 0.102 m | 0.066 / 0.127 m |
| → départ | 0.137 m | 0.065 / 0.144 m |

With the later fixes and the new contact monitor, WebRTC realism runs reached those four targets
at the following distances after stopping. Each also checked the skating refusal, mode changes
and manual movement before the trips.

| Seed | Bedroom | Salon | Bedroom again | Départ | World-box contact events |
|---|---:|---:|---:|---:|---:|
| 0 | 0.083 m | 0.147 m | 0.067 m | 0.187 m | 0 |
| 1 | 0.122 m | 0.081 m | 0.093 m | 0.163 m | 0 |
| 2 | 0.067 m | 0.154 m | 0.057 m | 0.172 m | 0 |

These are individual measurements, not guarantees. WebRTC itself does not expose contact data:
the `contacts` sample field is `null`, not zero. Older WebRTC traces that reported zero used a
placeholder and must not be used to claim collision-free operation. The current isolated TCP
and WebRTC harnesses separately instrument the simulator to record contacts with the scene's
world-box geometry at every physics step; the controller does not use this information. They do
not cover collisions with all possible non-box or dynamic geometry.

## Limits for this apartment

The recorded runs do not establish reliable room-to-room navigation across all starts, routes and
realism seeds. In particular, a small particle dispersion or a successful head sweep is not a
guarantee that the robot is at the estimated pose. Older measurements above used earlier versions
of the localization model and should not be read as validation of the current code.

The available sensors and map have important limitations:

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
