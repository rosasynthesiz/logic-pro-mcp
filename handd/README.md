# handd

Phase 1 sensor for the gesture-control system described in `../GESTURE_VOICE_PLAN.md`.

`handd` captures the Mac's camera, runs Apple's Vision framework hand-pose detector on every
frame, and streams normalised hand landmarks as one JSON object per UDP datagram to a local
listener (default `127.0.0.1:9101`) at up to 60 fps. It has no external dependencies — just
AVFoundation, Vision, and Foundation from the macOS SDK.

## Build

Requires macOS 14+ and a Swift 5.9+ toolchain (ships with recent Xcode / Xcode Command Line Tools).

```sh
cd handd
swift build -c release
```

The binary lands at `.build/release/handd`.

## Run

```sh
.build/release/handd                       # defaults: port 9101, host 127.0.0.1, 2 hands, 60fps, mirrored
.build/release/handd --port 9101 --max-hands 1
.build/release/handd --list-devices         # print available cameras and exit
.build/release/handd --device "Studio"      # pick a camera by substring match on its name
.build/release/handd --no-mirror --quiet
```

Stop it with Ctrl-C (SIGINT is handled cleanly — the capture session is stopped before exit).

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `--port <n>` | `9101` | UDP destination port |
| `--host <addr>` | `127.0.0.1` | UDP destination host |
| `--max-hands <n>` | `2` | Max simultaneous hands to track. Vision runs one full hand-pose inference pass per detected hand, so a higher count costs latency roughly linearly. |
| `--fps <n>` | `60` | Target emit rate. Frames are throttled in software to this rate; actual camera throughput may be lower if the hardware/format doesn't support it. |
| `--mirror` / `--no-mirror` | mirror **on** | When on, `x = 1 - x`, so on a user-facing camera "hand moves right" means x increases. Does not affect `chirality` (see below). |
| `--device <text>` | unspecified → system default | Case-insensitive substring match against a camera's name |
| `--list-devices` | — | Print `name<TAB>uniqueID` for every discovered camera (to stdout) and exit |
| `--quiet` | off | Suppress the startup banner |

## Wire format

One UTF-8 JSON object per UDP datagram, no trailing newline needed:

```json
{"t":12345.678,"source":"handd","hands":[{"chirality":"right","score":0.93,"lm":[[0.51,0.33,0.0], ... 21 triples ...]}]}
```

- **`t`** — seconds from `ProcessInfo.processInfo.systemUptime` (a monotonic clock, not wall time).
- **`source`** — always `"handd"`.
- **`hands`** — array of 0, 1, or `--max-hands` detected hands. **A frame with no hands still emits
  a datagram with `"hands":[]`.** This is intentional: the consumer needs to know the hand is
  *gone*, not just that packets stopped arriving. Downstream, "hand lost" should mean "freeze the
  last value," which only works if silence-vs-empty is unambiguous.
  - **`chirality`** — `"left"`, `"right"`, or `"unknown"`, from `VNHumanHandPoseObservation.chirality`.
    Not affected by `--mirror`: a front-facing camera is a projection of the scene (like someone
    looking at you), not an optical mirror, so Vision's handedness classification is already
    correct for the physical hand regardless of the x-flip applied for on-screen UX.
  - **`score`** — `observation.confidence`, 0.0-1.0.
  - **`lm`** — exactly 21 `[x, y, z]` triples, one per hand joint, in the fixed order below.
    - `x`, `y` are normalised 0..1. Vision reports `y` from the bottom of the image; `handd` flips
      it to `1 - y` so downstream code can assume a top-left screen origin.
    - `z` is always `0.0` — Vision gives no true depth. The triple shape is kept so a later
      higher-DOF sensor (Leap Motion, a ring) can fill it in without changing the wire format.

### Landmark order (index → joint)

Vision's own skeletal order matches MediaPipe's canonical 21-point hand layout — wrist, then each
finger proximal-to-distal:

| Index | Joint |
|---|---|
| 0 | wrist |
| 1 | thumb CMC |
| 2 | thumb MCP |
| 3 | thumb IP |
| 4 | thumb tip |
| 5 | index MCP |
| 6 | index PIP |
| 7 | index DIP |
| 8 | index tip |
| 9 | middle MCP |
| 10 | middle PIP |
| 11 | middle DIP |
| 12 | middle tip |
| 13 | ring MCP |
| 14 | ring PIP |
| 15 | ring DIP |
| 16 | ring tip |
| 17 | little MCP |
| 18 | little PIP |
| 19 | little DIP |
| 20 | little tip |

Note: Vision's API names index 2 `VNHumanHandPoseObservation.JointName.thumbMP`
(metacarpophalangeal), not `.thumbMCP` — same joint, different Apple naming convention for the
thumb specifically. The wire format calls it "thumb MCP" for consistency with the other fingers.

## Camera permission (macOS gotcha)

Command-line tools need explicit camera permission on macOS, tied to `NSCameraUsageDescription`.
`handd` calls `AVCaptureDevice.requestAccess(for: .video)` on startup, which triggers the system
permission prompt the first time you run it.

If you've previously denied it, or if you're running under a script/launcher that doesn't surface
the prompt, `handd` will print:

```
handd: camera access was denied.
Grant camera access in System Settings > Privacy & Security > Camera for the
terminal app you launched handd from (e.g. Terminal, iTerm), then re-run handd.
```

Camera permission on macOS is granted per **launching app** (Terminal.app, iTerm2, etc.), not per
binary — go to **System Settings > Privacy & Security > Camera** and enable the terminal app you
ran `handd` from, then re-run it.

## Design notes

- Capture uses `AVCaptureVideoDataOutput` on a dedicated serial dispatch queue, with
  `alwaysDiscardsLateVideoFrames = true`: a stale hand-position frame is worth less than the
  live one behind it in the queue, so dropping is the correct failure mode under load.
- `--fps` throttling happens in software (a minimum-interval check per frame), in addition to a
  best-effort attempt to set the camera's own frame duration toward the target rate.
- UDP is sent over a raw POSIX socket (`Darwin`'s `socket`/`sendto`), not `Network.framework`, to
  keep the dependency surface to exactly AVFoundation + Vision + Foundation as specified. Sends
  are fire-and-forget: a dropped datagram on localhost just costs the consumer one tick at 60fps.
