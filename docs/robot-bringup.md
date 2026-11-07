# Delivery-day bring-up: from the box to coin-coin

This runbook covers the first hour with a real MicroDuck, in order: provisioning, update, health,
policies, first motion, pointing coin-coin at the robot, and recording a session for replay.
Commands come from the upstream MicroDuck docs (`docs/robot/cheatsheet.md`,
`docs/robot/duckctl.md`, `docs/robot/telemetry.md`, `deploy/robotd.toml` at release 0.18.9).
Anything else is marked **(unverified)**. Read [sim-real-parity.md](sim-real-parity.md) first for
what will behave differently from the sim.

`<robot>` below is the robot's name, `duck-XXXX`, as shown by `duckctl scan`. `<ip>` is its LAN
address.

## 0. Before unboxing

- Install `duckctl` on the laptop. It is the `duckctl` crate in the MicroDuck repository, run
  with `cargo run -p duckctl -- …`, and it needs Bluetooth.
- Have the 2.4/5 GHz Wi-Fi SSID and passphrase ready. The robot needs internet access for NTP and
  updates: its RTC resets on a hard power-off, and TLS and updates need the correct time.
- Have a stand, a box or a hand ready to hold the robot with its feet off the ground. Clear about
  2 m² of flat floor away from stairs and table edges.
- **Do not pair a gamepad yet.** An idle pad sends zero velocities, which fight coin-coin's
  `robot.move`.

## 1. Unboxing and safety

1. Check the robot for loose cables and that the legs move freely by hand. Do this with the power
   **off**.
2. Charge or fit the battery. **(unverified: the charging procedure and LED meanings are not
   documented upstream.)**
3. Power on with the robot **seated or held**. When `robotd` comes up, the robot greets (quack).
   Silence after a minute means audio is off or there is no voice bank. It does not by itself
   mean the robot is broken.
4. Emergency stops, from most to least immediate:
   - pick the robot up: about 0.2 s later the policy pauses and the legs hold a stance;
   - stop sending commands: the robot stops by itself after the 500 ms deadman
     (`[safety] deadman_ms`);
   - over ssh, `sudo robotctl robot relax --yes` (torque off: the robot goes limp, so hold it);
   - the power switch.

## 2. Find the robot and provision Wi-Fi (BLE, `duckctl`)

```sh
duckctl scan                                            # robots in range: name, address
export DUCK_ROBOT=<robot>
export DUCK_PIN=<six digits>                            # see the PIN note below
duckctl wifi status
duckctl wifi connect "<ssid>" --psk "<passphrase>"
duckctl ip                                              # the robot's LAN address
```

PIN: on current releases the pairing PIN defaults to the **last six digits of the serial on the
label**. Older docs say `000000`. Once you have shell access, `robotctl system pin` shows it.

If mDNS works, the robot may also answer as `<robot>.local`, or with the fallback hostname
`radxa-zero3.local`. Upstream says mDNS is unreliable, so prefer `duckctl ip`.

## 3. Shell access and versions

```sh
duckctl ssh                       # or: ssh microduck@$(duckctl ip)
robotctl version                  # what every daemon runs vs what is installed
robotctl health                   # non-zero exit when unhealthy or unreachable
```

Write down the version line. coin-coin was developed against **0.18.4 / API v47**. Current upstream
is **0.18.9 / API v51**.

## 4. Update to the current release

```sh
robotctl update status
robotctl update check daemon
sudo robotctl update apply daemon     # the connection drops: that is the update working
robotctl version                      # after reconnecting
robotctl update log                   # if anything looks wrong
```

- **Disconnect coin-coin and any browser console first.** Since API v50, an update is refused with
  `SESSIONS_CONNECTED` while another WebRTC peer is connected.
- Automatic rollback is off by default. A release that fails its health gate stays live and is
  reported `stuck`. To go back, run `sudo robotctl update rollback daemon`.

## 5. Health and policies

```sh
robotctl health                   # battery, motors, thermal, control loop, camera
robotctl policy list
robotctl policy check
sudo robotctl policy update       # only if check reports a newer official set
robotctl monitor                  # live view; 't' shows the ToF matrix
```

Check the following:

- `healthy`, not `degraded`. `degraded` names a missing servo or IMU.
- The control loop runs at about 50 Hz. Below 45 Hz the robot is reported unhealthy.
- The battery percentage.
- The policy set is v9 on 0.18.9.
- The camera is not reported as stopped.

## 6. First motion, in this order

Use the gamepad-free path, over ssh on the robot. Run each step only when the previous one
behaved.

1. **Suspended.** Hold the robot or put it on its stand with the feet free.
   ```sh
   sudo robotctl robot init           # moves every joint to home; torque on
   sudo robotctl robot relax --yes    # torque off again: hold it
   ```
2. **Pickup detection.** Leave this running in a second shell during the tests below:
   ```sh
   journalctl -u robotd -f | grep -E 'picked up|put down|pickup'
   ```
   Lifting a driving robot should log `picked up — pausing the policy`, and putting it down should
   log `put down — handing the robot back`. The classifier was trained in simulation only. A pause
   while walking normally, or a robot that stays paused, is a finding to report.
3. **Stand, then stop.** `robotctl robot enable` is the gamepad's Start: it gives the robot to
   its policy, which stands it up. `robotctl robot enable --off` takes it back, and the robot
   holds its pose. Both come from the robotctl 0.18.9 source; **(unverified: whether `sudo` is
   needed)**. coin-coin itself sends `robot.enable {"on": true}` over WebRTC.
   ```sh
   robotctl robot enable              # stands up from sitting
   robotctl robot mode                # walk or roller; changes nothing
   robotctl robot enable --off        # policy off, pose held
   ```
   `robot.stop` is refused over BLE (`btd`) on purpose, because a radio stop is not an e-stop.
   Test stop from coin-coin over WebRTC in step 5: the robot must stop and keep standing.
4. **Deadman.** From coin-coin or any client, send a single `robot.move {vx: 0.1}` and nothing
   else. The robot must stop within about 0.5 s.
5. **Short walk.** Do this on the floor, through coin-coin (step 7), with `MAX_MOVE_S` at 2 s and
   the default speed limits. Ask for a slow forward step and a slow turn.
6. **Mode change (roller).** This is **not available remotely**: mediad refuses `robot.setMode`
   over WebRTC, and btd refuses it over BLE. The cheatsheet shows
   `duckctl call robot.setMode '{"mode":"roller"}'`, but btd 0.18.9 refuses
   `RobotSetMode`, so expect a refusal **(unverified)**. The documented, persistent way:
   ```sh
   sudo robotctl configure            # [policy] mode → roller, accept the robotd restart
   robotctl robot mode                # check it
   ```
   A runtime `robot.setMode` sent on the robot to robotd's Unix socket also works (one quack =
   walk, two = roller) and is not persistent, but no `robotctl` subcommand wraps it
   **(unverified)**.

## 7. Point coin-coin at the robot

```sh
cd ~/projects/microduck/coin-coin
export ROBOT_LINK=webrtc://<ip>:8443      # mediad signalling port
```

1. Run the coin-coin connection check built by the real-link stream (a `coincoin doctor`-style
   command; see its README). It should report:
   - signalling reachable;
   - `control` datachannel open;
   - the `hello` API version;
   - `robot.state` arriving at about 10 Hz;
   - a video frame decoded;
   - `robot.policies` and `robot.mode` readable.
2. Run the offline protocol check against the version the robot reported:
   ```sh
   uv run pytest tests/contract -q --no-cov
   ```
3. Start coin-coin normally with that `ROBOT_LINK`. Do **not** use `scripts/live_stack.py`: it
   starts a simulator and forces a localhost link.

The browser console at `http://<ip>:8080/` (`duckctl open`) is another WebRTC peer. Close it while
coin-coin drives.

## 8. Record a session for replay

Record 2 minutes on the real robot. The recorder sends no motion commands. Drive with coin-coin
or by hand, and type notes as you go:

```sh
uv run python -m scripts.record_session --link webrtc://<ip>:8443 \
    --duration 120 --out recordings/day1-walk.jsonl --notes --label "day1 parquet walk"
# type e.g. "lifted", "put down", "walk 1 m", "pushed" + Enter while it records
```

Suggested recordings, each 1–2 minutes:

- idle standing;
- lifted and put down 3 times;
- a short walk with stops;
- slow turns;
- (roller) push and brake.

Optional ToF: tofd's socket is `/run/tofd/tof.sock`. Forward it over ssh with `socat`, then pass
`--tof tcp://127.0.0.1:<port>` **(unverified)**.

Afterwards:

```sh
uv run python -m scripts.replay_session summary recordings/day1-walk.jsonl
uv run python -m scripts.replay_session compare recordings/sim-walk.jsonl recordings/day1-walk.jsonl
uv run python -m scripts.replay_session serve recordings/day1-walk.jsonl --port 7810
# then: ROBOT_LINK=tcp://127.0.0.1:7810 runs coin-coin against the recording, offline
```

In tests, `scripts.session_log.ReplayLink("recordings/day1-walk.jsonl", clock=...)` is a drop-in
`RobotLink`.

Recordings contain camera snapshots of your home. Keep them out of Git; `recordings/` is not
tracked.

## 9. What only the hardware can validate

- Servo bus (Dynamixel) behaviour: overload trips, `reboot-motors`, a missing servo reported as
  `degraded`.
- The real gait on your floor: slip, skate glide in roller mode, rugs.
- The pickup classifier, and fall detection with real IMU noise.
- Camera colour, exposure, motion blur, FOV, and H.264 artefacts over your Wi-Fi.
- ToF status codes on dark, shiny or glass surfaces.
- The robot's own voice and volume. The robot mic is **not** streamed to clients (mediad has no
  audio track), so coin-coin keeps using the laptop mic.
- Battery autonomy and the low-battery sit-down. Use `sudo robotctl endurance` on clear floor
  with no pad. Logs go to `/var/lib/endurance/`.
- Thermal throttling (95 °C → 408 MHz) and the control-loop rate under sustained use.
- Wi-Fi latency and dropouts in your home, measured against the 500 ms deadman.

## Logs worth keeping from day 1

```sh
robotctl health --json > day1-health.json
robotctl logs robotd -n 300 > day1-robotd.log
robotctl monitor --json --hz 50 > day1-monitor.jsonl     # Ctrl-C to stop
```

---

## Résumé en français

1. **Avant** : installer `duckctl` sur le portable, préparer le Wi-Fi (avec accès internet, pour
   NTP et les mises à jour) et un support pour tenir le robot pattes en l'air. Ne pas appairer
   de manette.
2. **Sécurité** : soulever le robot met la politique en pause (~0,2 s). Couper les commandes
   l'arrête en 0,5 s (deadman). `sudo robotctl robot relax --yes` coupe le couple : tenir le
   robot.
3. **Wi-Fi par BLE** : `duckctl scan`, `export DUCK_ROBOT=… DUCK_PIN=…`, puis
   `duckctl wifi connect "<ssid>" --psk "<mdp>"` et `duckctl ip`. Le PIN correspond aux six
   derniers chiffres du numéro de série sur l'étiquette, ou à `robotctl system pin`.
4. **Versions et mise à jour** : `robotctl version`, puis `robotctl update check daemon` et
   `sudo robotctl update apply daemon`, **coin-coin déconnecté** (sinon `SESSIONS_CONNECTED`).
5. **Santé et politiques** : `robotctl health` (`healthy`, ~50 Hz, batterie, caméra),
   `robotctl policy check`.
6. **Premiers mouvements** : suspendu (`robot init` / `relax`), détection de soulèvement,
   `robotctl robot enable`, stop et deadman depuis coin-coin, courte marche. Le changement de
   mode (roller) est refusé par WebRTC **et** par BLE : utiliser `sudo robotctl configure`
   (`[policy] mode`).
7. **coin-coin** : `ROBOT_LINK=webrtc://<ip>:8443`, lancer la vérification de connexion de
   l'équipe real-link, puis `uv run pytest tests/contract`. Fermer la console navigateur.
8. **Enregistrer** 2 minutes avec `scripts.record_session`, puis rejouer hors ligne avec
   `scripts.replay_session` (`summary`, `compare`, `serve`).
9. **Seul le vrai robot valide** : servos, glisse réelle, classifieur de soulèvement, caméra,
   ToF, voix, autonomie, chauffe et Wi-Fi. Le micro du robot n'est pas transmis.
