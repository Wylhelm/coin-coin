# Sim/real parity audit

The tables below are a historical audit. See the integration status in section 4 and
[localization.md](localization.md) for implemented navigation behavior and remaining limits.

What separates the coin-coin simulation stack (`scripts/live_stack.py`, `robotd --sim`,
`microduck_rl` body server, TCP relay) from a real MicroDuck. It also lists what would slow down
the first connection on delivery day. This page covers what is **not** already handled by the
four parallel work items: honest localization, obstacle avoidance, the real WebRTC path, and
sensor noise/slip/dropouts/no-assist.

Sources: coin-coin `sim/parity-bringup` (from `main`), the local MicroDuck fork (`HEAD` 32a7a8b5,
release 0.18.4, API v47, policy set v8), and upstream MicroDuck `origin/main` da34a221 (release
0.18.9, API v51, policy set v9). Upstream files were read with `git show origin/main:<path>`.
Rows marked **unverified** come from code or docs only and have not been measured on a robot.

Tools added with this audit:

- `tests/contract/test_protocol_contract.py`: checks coin-coin's JSON-RPC methods, literal
  parameter keys and sound tags against `duck-ipc-proto`. It also checks mediad's WebRTC route
  refusals. It runs offline and skips when no MicroDuck checkout is available.
- `scripts/record_session.py`, `scripts/replay_session.py` and `scripts/session_log.py`: record a
  real robot for about 2 minutes, then summarize it, compare it with a sim recording, replay it
  in-process (`ReplayLink`) or serve it as a TCP robotd. See [robot-bringup.md](robot-bringup.md).

## 1. Critical API differences with upstream `main`

coin-coin was written against the local fork at API **v47**. A robot updated to the current
release speaks **v51**. The robot accepts an older `hello`, because the versions only add
features, so the gap does not stop a connection. The behaviour changes below matter more.

| Change | Version | Impact on coin-coin | Action |
| --- | --- | --- | --- |
| `robot.setMode` refused over WebRTC (`mediad/src/route.rs::permits`, "a decision made in the room") | all releases | `Governor.set_mode` / the walk↔roller tool cannot work on the real remote path. It gets `METHOD_NOT_FOUND`: "robot.setMode is not available over WebRTC" | Report as a human step on the robot (`sudo robotctl configure` → `[policy] mode`). btd also refuses it over BLE, although the cheatsheet shows `duckctl call robot.setMode`. The real-link stream owns this. The contract test pins it as the only refused method coin-coin uses. |
| `PoliciesResult.simulated` exists only in the local fork | fork only | The real robot never sends it, so `simulated is True` is false. Fallen-mode-switch recovery is disabled, which fails safe. | None needed. Never rely on a positive `simulated`. |
| `robot.stop` cancelling a pending mode switch (`MODE_CANCELLED`), seated carry-over, `zero_pad_gait_pose` for roller, sim `set_drive_mode`, mode switch while fallen | fork only | Behaviour tested in sim does not exist on the robot. On a real robot, stop does **not** cancel a mode switch already running. | Keep these out of any acceptance criterion meant for hardware. Upstream them or drop them. |
| `robot.remoteSessionActive` removed | v50 | coin-coin does not use it (contract test guards this). | None. |
| `update.apply` / `os.apply` / `os.switch` refused with `SESSIONS_CONNECTED` while another WebRTC peer is connected | v50 | A coin-coin connected over WebRTC blocks OTA updates started by someone else. | Disconnect coin-coin before updating (runbook step). Unless the caller passes `interrupt_sessions`, the update waits. |
| Health is `degraded`, not `unhealthy`, when a servo or the IMU goes missing mid-run | v48 | Any health gate in coin-coin must treat `degraded` as "do not drive". | Today coin-coin reads `robot.health` only for status. |
| Emotions are skills (`robot.do sad`, `yes`, `no`, `excited`, …), and the pad's Y/LB/RB are bound to them | 0.18.x | `robot.skills` lists more entries. A pad press plays an emotion that takes the head, pose and beak, and another `robot.do` is dropped until it ends. | Expect skills that coin-coin did not ask for. Do not treat a dropped `robot.do` as a link fault. |
| Roller: `accel_vx` 1.0 m/s² ramp, idle look-around off | 0.18.x | A roller command reaches its speed about 1 s later than in the fork sim. Odometry-based checks must allow for this. | Re-run roller sim checks after the fork rebase. |
| Policy set pinned to **v9** (v7's velstand, because v8 was worse) | 0.18.9 | The fork sim walks with v8. Gait, speed tracking and stand behaviour differ from the robot. | Seed v9 in the sim (`scripts/seed-policies.sh`) after rebase. |
| `pad.pair` candidates, NFC pairing tag, pairing PIN default = last six digits of the label serial | v51 / 0.18.x | The duckctl docs still say `000000`. The first `duckctl` command may fail authentication. | Runbook: get the PIN from the label or `robotctl system pin`, then `export DUCK_PIN=…`. |
| Automatic rollback off unless opted in (`auto_rollback`) | 0.18.x | A bad update stays live and is reported `stuck`. | Runbook: `robotctl update log`, `sudo robotctl update rollback daemon`. |
| mediad H.264 encoder capped at 2 pending frames ("camera stops" fix). `robotctl health` reports a stopped camera. | 0.18.x | On older releases the WebRTC video can freeze. | Update before testing vision. |

## 2. Parity matrix

Risk: **H** blocks or endangers a first session, **M** gives wrong behaviour that can be
recovered, **L** is cosmetic or slow.

### Transport, protocol and launcher

| Feature | Sim behaviour | Real robot behaviour | Risk | How to test in sim | Hardware only |
| --- | --- | --- | --- | --- | --- |
| Link | `tcp://` to `relay/duck_relay.py` → robotd Unix socket, with an optional token line | `webrtc://<robot>:8443`, through mediad signalling and the `control` datachannel. The relay is not used. | H | Real-link stream (WebRTC against `duck-sim`'s mediad) | NAT/Wi-Fi, DTLS on the robot |
| Method allowlist | Relay `ROBOT_METHODS` + `FRAME_METHODS` | mediad `permits()`: `robot.setMode` refused; `tof.stream`, `robot.health` and `robot.mode` permitted | H | `tests/contract` (added) | — |
| Camera frames | `media.frame` on the frame port: raw UYVY 640×360 from the sim renderer at 15 fps, converted to JPEG | H.264 video track (default 720p30, 2 Mb/s, GCC congestion control) decoded in coin-coin. `media.frame` does not exist over WebRTC. | H | WebRTC sim path (mediad `--sim-camera`) | ISP colour, exposure, blur, compression artefacts, real FOV |
| `hello` API version | 47 = fork 47 | 51 (0.18.9) | M | Contract test reports the gap | — |
| `robot.subscribe {hz:10}` | 10 Hz over localhost | 10 Hz over Wi-Fi datachannel (ordered). Head-of-line blocking happens on packet loss. | M | Add latency/loss to the WebRTC sim (netem) | Real Wi-Fi |
| Governor staleness (state older than 1 s → refuse) | Never triggers on localhost | Can trigger on Wi-Fi roaming or microwave interference | M | netem jitter (see work item 2) | Real home Wi-Fi |
| Deadman (`[safety] deadman_ms` 500, stop rather than limp) | Same robotd code, but notifications arrive in under 1 ms | `robot.move` at `move_hz` 20 means 50 ms periods; more than 500 ms of loss means the robot stops while coin-coin thinks it is moving | M | netem 600 ms blackout during `move` | Real Wi-Fi |
| `SIM_POSE_LINK` / `go_to` | Ground-truth pose from the simulator (sim-only) | Not available | H | Localization stream | — |
| `NAV_MAP` | Built from the sim scene | Must be rebuilt from the real room | M | Localization stream | Room survey |
| `walk_assist` | Off by default; a sim aid | Must stay off | L | Already off | — |
| Launcher (`live_stack.py`) | Starts duck-sim, the relay and coin-coin, and forces `ROBOT_LINK=tcp://127.0.0.1:7810` | There is no launcher for the robot. coin-coin runs alone with `ROBOT_LINK=webrtc://…` | M | — | First real start |
| Multiple ducks | `DUCK_SIM_DUCKS=1` | Several robots on the LAN, each with its own mediad and name `duck-XXXX` | M | `DUCK_SIM_DUCKS=2` and point at the second one | Real LAN discovery |
| Time | Same host clock | Robot RTC resets on hard power-off and needs NTP (needed for TLS and updates). `captured_at` / `t_ns` are on different clocks. | M | Skew the frame timestamp in a fake | Robot without internet |

### Locomotion and safety

| Feature | Sim behaviour | Real robot behaviour | Risk | How to test in sim | Hardware only |
| --- | --- | --- | --- | --- | --- |
| Policy set | v8 (fork) | v9 (velstand from v7) | M | Seed v9 in the sim | Real gait |
| Mode names | `walk`, `roller` | Same, but switching is not possible remotely | H | Contract test | — |
| Mode switch duration | A few seconds, deterministic | "A few seconds", with torque on and quacks (1 for walk, 2 for roller). The robot must physically have wheels. | M | — | Fitting the wheels |
| Fall detection | `safety.fallen` from sim gravity, debounce 200 ms; report only by default | Same code, real IMU noise. `limp_fall` is off by default. | M | Push or tilt the sim duck | Real falls (on soft ground) |
| Pickup detection | Classifier trained in sim, on by default; `safety.picked_up` and the policy reads `held` | Same classifier on a real IMU. Known risk: a false pause while walking, or a robot that stays paused after being put down. | M | Lift the duck in the MuJoCo viewer | **Yes**: the classifier was never validated on hardware |
| Control loop | 50 Hz as long as the host keeps up; health is unhealthy below 45 Hz | 50 Hz on RK3566. Thermal throttle at 95 °C (408 MHz) can drop it. | M | Throttle the sim CPU and check `loop.hz` in state | Thermal |
| Servo faults | Not modelled (no Dynamixel bus) | Overload trip, missing servo → `degraded` (v48), `robot reboot-motors` | M | — | **Yes** |
| Roller acceleration | No ramp (fork) | `accel_vx` 1.0 m/s² | L | After rebase | — |
| Ground | Rigid plane with fixed friction | Parquet, rugs, tiles; skates glide | M | Friction sweep (sensor/slip stream) | **Yes**: real skate glide |
| Stand and sit | `robot.do sit_toggle`, `policies.sitting` | Same, with real timing | L | — | Timing |

### Power, thermal and health

| Feature | Sim behaviour | Real robot behaviour | Risk | How to test in sim | Hardware only |
| --- | --- | --- | --- | --- | --- |
| Battery | Probably absent: the sim body has no voltage model, so `robot.health.battery` would be `null` (**unverified**; check `scripts/duck-sim ctl health`) | `battery {volts, percent}`, a 10 s EMA. At 6.6 V robotd sits the robot down and powers off (`battery_empty_shutdown`). `voltage_adapt` scales gains around 7.4 V. | H | Fake health in coin-coin tests; a sim battery model (work item 5) | Autonomy, sag under load |
| Battery in `robot.state` | — | Not in state; only in `robot.health` | M | coin-coin should poll health (see §4) | — |
| Thermal | None | `motors.max_c`, `cpu_temp_c`, `cpu_throttle` in health. `look` is rate-limited to 2 Hz for thermal reasons. | M | Fake health | **Yes** |
| Health verdict | `healthy` | `healthy` / `degraded` / unhealthy, with a reason | M | Fake health | — |

### Audio and expression

| Feature | Sim behaviour | Real robot behaviour | Risk | How to test in sim | Hardware only |
| --- | --- | --- | --- | --- | --- |
| Microphone | Laptop/browser mic in coin-coin | The robot has an onboard mic (TLV320AIC3104 codec), but **mediad sends no audio track**. `WebRtcLink._on_track` also stops non-video tracks. The robot mic cannot reach coin-coin. | H for "talk to the duck" | — | Far-field audio from the robot does not exist today; the laptop or phone mic stays the input |
| Speaker | `QUACK_SINK=stream` plays coin-coin's bank in the browser. `robot` sends `robot.sound {tag, hold}`. | The robot plays its **own** per-serial voice bank (tags alarm, greet, inquire, peck, chirp, coo, wheee). There is no way to stream speech to the robot speaker. | M | Contract test checks the tags | Volume and how the voice sounds |
| Startup greet, goodbye peck, pet coo (opt-in) | Sim sound depends on duck-sim audio | Plays automatically, so coin-coin may hear the robot's own quack through the laptop mic | L | — | Echo |
| Emotions | Fork: probably absent (**unverified**) | 13 emotions as skills with sounds | L | After rebase | — |
| Face LEDs and flashlight | Not used by coin-coin | `robot.flashlight` is permitted over WebRTC | L | — | — |

### Sensors

| Feature | Sim behaviour | Real robot behaviour | Risk | How to test in sim | Hardware only |
| --- | --- | --- | --- | --- | --- |
| ToF | Sim `tof.py`: 8×8, 45° FOV | VL53L8CX, 15 Hz, `distance_mm` + `status` (`x` means could not measure). Served by `tofd` on its own socket. `tof.stream` is permitted over WebRTC but **missing from coin-coin's `ROBOT_METHODS`**. | M | Obstacle stream | Status codes on dark or shiny surfaces |
| Camera model | MuJoCo pinhole `Intrinsics::sim` | imx219 (zero3) or gc2093 (beta); rotation; real FOV and distortion | M | — | Calibration |
| Head IMU | Sim | BMI088 or LSM6DSV16X depending on the board | L | — | — |

### Operations

| Feature | Sim behaviour | Real robot behaviour | Risk | How to test in sim | Hardware only |
| --- | --- | --- | --- | --- | --- |
| Updates (OTA) | None | `updaterd`; the connection drops after an apply. With a peer connected: `SESSIONS_CONNECTED`. | M | Fake a link drop in the middle of a recording (`ReplayLink` with `error` lines) | Real update |
| Provisioning | None | BLE (`duckctl`), Wi-Fi via `configd`, PIN | H (delivery-day time) | — | **Yes** |
| Pad | `padd` in the sim, optional | A paired pad sends zero velocities when idle, which cancel or compete with coin-coin's `robot.move` | M | — | Disconnect or don't pair the pad during coin-coin tests, as the endurance doc advises |

## 3. Recommended simulation work beyond the four in-flight items (ranked by value)

1. **Rebase the sim runtime on upstream 0.18.9 (API v51, policy set v9).** Every behaviour
   measured in sim today runs on v8 policies and fork-only features (stop cancels a mode switch,
   fallen mode switch). Until this is done, sim results do not predict the robot.
2. **Wi-Fi latency, jitter and loss on the control channel.** Use netem on the WebRTC sim path,
   with a profile of 20–80 ms RTT, 1–3 % loss and occasional 300–800 ms blackouts. Assert three
   things: the 500 ms deadman stops the robot, the governor's 1 s staleness refuses, and
   coin-coin's `move` reports truthfully. Ordered datachannel head-of-line blocking is the case
   the localhost sim can never show.
3. **Mode change as a human step.** Simulate `robot.setMode` refused remotely. coin-coin should
   ask the user to switch mode and then detect the change by polling `robot.mode`. Remove any
   reliance on stop cancelling a mode switch.
4. **Pickup and fall parity scenarios.** Lift, put down and tip over the sim duck with real-like
   IMU noise. Check that coin-coin refuses motion while `picked_up` is true, resumes after
   put-down, and never retries on `fallen`. Record the same scenarios on hardware on day 1 and
   compare them with `replay_session compare`.
5. **Battery model and low-battery behaviour.** Add a voltage curve and sag under load to the sim
   body, so `robot.health.battery` exists and `battery_empty_shutdown` (sit + power-off) can be
   triggered. coin-coin should poll health (30 s), announce low battery and stop planning long
   moves below a threshold.
6. **Thermal throttle and control-loop degradation.** Inject a `loop.hz` drop below 45 Hz and
   `cpu_throttle` in health. coin-coin should slow down or refuse long walks and say why.
7. **Camera realism on the WebRTC path.** Stream the sim camera at 720p30 through H.264 at 2 Mb/s
   with GCC. Add motion blur and exposure changes, and simulate "camera stopped" (no frames for
   more than 2 s). Vision tools must report "camera stopped" instead of describing a frozen frame.
8. **Robot speaker vs browser audio.** Run coin-coin with `QUACK_SINK=robot` in sim, and handle the
   robot's own greet/peck heard by the laptop mic (wake-word false positives). Document that the
   robot mic is not streamed, so far-field robot audio needs a mediad change first.
9. **OTA in the middle of a session.** Replay a recording that ends in a link drop followed by a
   new `hello` that reports a higher daemon version. coin-coin must reconnect and re-read
   policies and skills. It must disconnect when the user asks for an update (`SESSIONS_CONNECTED`).
10. **Several ducks on one LAN.** Use `DUCK_SIM_DUCKS=2` behind one mediad (producer selection) or
    two. coin-coin must bind to one named robot and refuse to drive an unexpected one.
11. **Time sync.** Skew robot and host clocks (RTC reset with no NTP). Frame-age logic must use
    receive time, not `captured_at`, across hosts.
12. **Emotion and pad interference.** A pad press plays an emotion or sends zero velocities while
    coin-coin drives. Check that coin-coin tolerates a dropped `robot.do` and competing twists.
13. **Servo dropout → `degraded`.** Report a degraded verdict mid-walk and check that coin-coin
    stops and explains.

## 4. Required changes in existing files (not made here, to avoid conflicts)

> **Integration status:** done — API 52 with 47 as minimum (doctor range check),
> `tof.stream` in `ROBOT_METHODS` with ToF over the WebRTC channel (verified in sim),
> `robot.health` polled every 5 s with `degraded` stopping and refusing motion,
> `robot.setMode` refusal handled as a person step (real-link). Upstream items are in
> `docs/upstream-requests.md`.

- `src/coincoin/robot/jsonrpc.py`: `API_VERSION = 47`. After the fork rebase, raise it to 51.
  Optionally log `daemon_version` and `revision` from the `hello` result for the bring-up
  report.
- `src/coincoin/robot/methods.py`: add `tof.stream` if the obstacle stream reads ToF over WebRTC.
  mediad permits it, and the `tof.frame` notifications then arrive on the same channel
  (**unverified**: tofd is a separate socket, and the route sends it to that service).
- Governor and set-mode tool: treat `METHOD_NOT_FOUND … not available over WebRTC` from
  `robot.setMode` as a human step. Do not rely on `robot.stop` cancelling a mode switch on real
  hardware.
- Status and health: poll `robot.health` every 30 s and expose battery, thermal and
  `healthy`/`degraded`. Refuse motion when the robot is `degraded`.
- `webrtc_link.py`: no change is needed for audio, because mediad offers no audio track. Robot
  mic audio needs a mediad change upstream first.
- `README` / `docs/webrtc.md`: note that the pairing PIN defaults to the last six digits of the
  label serial on current releases. The duckctl docs still say `000000`.
- `pyproject.toml`: add `scripts` to pyright `include` if the new scripts should be type-checked
  in CI. Today they are checked only through the tests that import them.
