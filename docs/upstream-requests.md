# Requests for the MicroDuck runtime (Pollen Robotics)

coin-coin works around each item below on its own side. None of them is implemented in the
MicroDuck runtime by this project. This list is ready to send upstream. The local fork stays on
upstream behaviour so the simulator keeps predicting the delivered robot.

Context: coin-coin drives a MicroDuck through mediad's WebRTC `control` datachannel, the same path
as the console, and through `robotd`'s JSON-RPC (API v47–v52). The details are in
[sim-real-parity.md](sim-real-parity.md), [webrtc.md](webrtc.md) and
[robot-bringup.md](robot-bringup.md).

## 1. Report an API version mismatch in the `hello` reply

**Today.** `hello {api_version}` is accepted whatever version the client sends. The reply does not
say whether the daemon considers the client older, newer, or compatible. A client written for v47
talking to v52 learns about removed or changed methods only when a call fails.

**Request.** Add `min_api_version` (the oldest client the daemon still serves faithfully) to the
`hello` result, or a `compatible: bool` plus a short `note`. Clients could then refuse or warn
before driving.

**coin-coin today.** It sends `API_VERSION = 52` and accepts any daemon from v47 through v52. Below
v47, `coincoin-cli doctor` fails. Above v52 it warns.

## 2. Query which methods a route permits

**Today.** mediad's `route.rs::permits()` decides per method what a WebRTC peer may call; for
example, `robot.setMode` is refused as "a decision made in the room". A client discovers a refusal
only by calling the method and getting `METHOD_NOT_FOUND`, which a motion method should never have
to do.

**Request.** A read-only `route.capabilities` call, or a field in the `hello` result, that lists
the permitted methods (and streams such as `tof.stream`) for the current transport: WebRTC, BLE or
a local socket.

**coin-coin today.** A capability probe at connect time (`capabilities.py`) plus a contract test
that pins the known refusal set against `route.rs`.

## 3. A documented procedure for mode and equipment changes from the pad or BLE

**Today.** `robot.setMode` is refused over WebRTC (mediad) and over BLE (btd), even though the
cheatsheet shows `duckctl call robot.setMode`. The only remote-free procedure we found is
`sudo robotctl configure` → `[policy] mode` on the duck, which needs a shell on the robot.

**Request.**

- Document the supported in-the-room way to switch between walk and roller, including fitting or
  removing the skates. A pad button combination, a BLE command, or a physical gesture would all
  work.
- Fix the cheatsheet, or allow `robot.setMode` over BLE.

**coin-coin today.** It asks a person, in generic words, to fit or remove the equipment and to
select the mode on the robot. It then waits (`PERSON_MODE_TIMEOUT_S`) until `robot.mode` reports
the change.

## 4. Confirm `tof.stream` over the WebRTC control channel

**Today.** `permits()` allows `TofStream` and routes it to the tofd lane. coin-coin now subscribes
over the datachannel and reads `tof.frame` notifications there, which feeds its odometry + ToF
localization. In the simulator the subscription works. It is not documented as a supported
client interface.

**Request.** Confirm this is supported and stable, including:

- the expected frame rate over Wi-Fi;
- whether one subscription per control channel is the contract;
- whether a remote (rendezvous) control lane would ration these notifications. `relay.rs` has a
  `Budget` of 400 notifications per 60 s, below 15 Hz.

## 5. An audio track from the robot microphone

**Today.** mediad offers only a video track, so the onboard microphone cannot reach a WebRTC
client. Voice control has to use the laptop's or phone's microphone.

**Request.** An optional Opus audio track from the robot microphone on the WebRTC session.

## 6. A battery model in the simulator body

**Today.** In `duck-sim`, `robot.health.battery` is absent or `null`. Low-battery behaviour
(`battery_empty_shutdown` at 6.6 V) cannot be exercised before hardware.

**Request.** A simple voltage curve with sag under load in the simulated body, so
`robot.health.battery` exists and shutdown can be triggered in simulation.

## 7. Speed floors of the v9 walking policy

**Today.** With policy set v9, the duck does not move for forward commands below about 0.2 m/s
or yaw commands below about 0.8 rad/s. It stands and steps in place.

**Request.** Publish the commanded-velocity envelope each policy set actually tracks, as minimum
and maximum per axis, for example in `robot.policies`. Clients could then clamp to it instead of
measuring it.

**coin-coin today.** It raises non-zero walking commands to `MIN_VX`, `MIN_VY` and `MIN_VYAW`,
and tells the user it did.
