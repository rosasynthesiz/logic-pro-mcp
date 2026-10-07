# Gesture and voice control

Built 2026-09-20. Research and the decision record are in `../GESTURE_VOICE_PLAN.md`; this file is how
it actually works and how to run it.

## The idea in one line

**Voice carries the nouns and the verbs. The hand carries the value.** Say *"volume on strings"* and
your hand becomes that fader until you let go.

Everything else follows from that split. A DAW has ~1000 commands and ~100 tracks; a hand has maybe six
poses you can hit without looking. So the hand never names anything — it only sets a number that voice
already gave a meaning to.

## Four processes

```
handd  (Swift, camera)  --UDP 9101-->  gestured  --unix sock-->  logicpadd  --UDP 7000--> Logic
voiced (Python, mic)    --UDP 9102-->      |                          ^
                                           +---UDP 9103--> hud        |
                                                                logic-mcp (the MCP server)
```

Four rather than one because each of the three edge processes can hang, crash, or sit behind a
permission prompt, and none of them should be able to take the control loop down. UDP between the
sensors and `gestured` is deliberate: a hand frame that arrives late is worth less than the frame behind
it, so **dropping is the correct failure mode**. The control hop to `logicpadd` is a unix socket, where
every message must land exactly once.

| Process | What it owns | Entry point |
|---|---|---|
| `handd` | the camera and Vision | `handd/` (Swift; `swift build -c release`) |
| `voiced` | the microphone and the pedal | `voiced` |
| `gestured` | the arbiter — the only thing that decides | `gestured` |
| `logicpadd` | **the one OSC socket and Bonjour advert** | `logicpadd` |
| `hud` | an always-on-top readout | `gesture-hud` |

## Why logicpadd exists

`channels/osc.py` binds UDP 9000 and holds a `dns-sd` advert. Only one process on the machine can, and
the module docstring records a measured fact: **Logic segfaults (SIGSEGV, 12.3) when the Bonjour service
of a bound OSC device disappears.** So the gesture daemon cannot just construct its own `LogicPad`.

`logicpadd` owns the one pad and serves it over a unix socket. `RemoteLogicPad` has the same public
surface as `LogicPad`, so `_osc_ready` in the server holds either without caring which. `forget()` is
deliberately **not** exposed over the socket — withdrawing the advert is the segfault.

With no daemon running, `acquire()` falls through to a direct `LogicPad` and behaviour is exactly what
it was before. Starting the daemon is an upgrade, never a requirement.

## The arbiter's three rules

`arbiter.py` is a pure state machine — no IO, no imports outside the package — because this is where a
mistake mutes the wrong track or fires Stop mid-take. 33 tests, no Logic required.

```
IDLE  --voice names a track + axis-->  ARMED
ARMED --clutch closes--------------->  ENGAGED     the hand now owns the axis
ENGAGED --clutch opens------------->  FROZEN       the value stays exactly where it was left
FROZEN --clutch closes------------->  ENGAGED      re-grab the same target
FROZEN --4 s--------------------->  IDLE
ARMED  --10 s-------------------->  IDLE
```

1. **No target, no motion.** A hand waving in IDLE does nothing continuous, and naming is not touching —
   even ARMED does nothing until the clutch closes.
2. **Discrete poses are suppressed while ENGAGED**, and also in the same transition as a clutch release.
   Fingers uncurl out of a pinch through something the classifier reads for one frame as a fist; without
   this rule every long ride would eventually stop the transport.
3. **Release freezes, never resets.** Losing the hand entirely is treated identically. A dropped camera
   frame must never read as "the hand moved to the bottom of the box".

## Poses

Six, and that is the ceiling. Each needs a dwell and hysteresis, or it fires while you reach for coffee.

| Pose | Does | Channel |
|---|---|---|
| open palm | play | OSC |
| fist | stop | OSC |
| **pinch** | **the clutch** — hold to engage | — |
| point + dwell | select the track under the pointer | MCU select |
| swipe left | undo | ⌘Z |
| two-hand spread | zoom the arrange window | key |

## Axes

All behind the clutch, all straight out as one OSC datagram — no readback on this path, because at 60 Hz
the next frame is the verification and a round trip inside the fader loop is exactly what the design
exists to avoid.

| Motion | Axis | Address |
|---|---|---|
| hand height in the calibration box | volume | `/1/volume<n>` |
| wrist roll | pan | `/2/pan<n>` |
| pinch gap | focused plug-in parameter | `/3/par<n>` |
| hand x | scrub | MCU jog (no OSC address) |
| two-hand x/y | EQ | `/4/...` (not wired yet) |

**Soft takeover** is on by default (`--takeover catch`): engaging does not snap the fader to the hand; it
walks from where Logic's fader *is* toward the hand at `CATCH_RATE` (full travel per second), so a big gap
is a short catch-up rather than a jump. `--takeover jump` is the plain-slider behaviour.

**Calibration is required.** "Hand height" means nothing until the box is pinned to your desk; the
default box is the middle of the frame and is a starting point, not a setting — on a MacBook camera, which
looks up at a face, a hand held above the chest read as ≤ 0.2 of fader travel with it. With `handd` running:

```bash
gestured --calibrate 15            # move low → high (and left → right); prints the --box it saw
gestured --box 0.2,0.45,0.8,0.95   # then run with it; the bottom edge is fader 0, the top edge fader 1
```

## Track resolution is fail-closed

The OSC/MCU surface only ever addresses the eight strips Logic is currently showing. `Executor.resolve`
matches exact, then prefix, then substring, and **refuses on ambiguity** — "strings" never quietly
becomes "string pad". This repo has been bitten once by code that assumed the bank and moved faders on
tracks nobody asked for.

## Region trim (`regions.py`)

The thing the gesture layer can do that no control surface can. `ax_logic.list_regions` gives every
region's real screen rectangle; `LiveDrag` presses on an edge, follows the hand while the clutch is
held, and releases.

The guard matters more than the feature: grabbing a few pixels inside the edge **moves** the region
instead of trimming it. So both edges are recorded first and checked after — if the edge we did not
touch moved, that was a move, and the result is `failed` and taken back with ⌘Z.

## Running it

```bash
uv sync
logicpadd &                       # start this FIRST — it owns the OSC surface
gestured --dry-run                # decides everything, sends nothing
gesture-fakehand                  # a scripted synthetic hand, no camera needed
gesture-hud                       # the readout
```

With a camera:

```bash
cd handd && swift build -c release
./.build/release/handd            # grant Camera to the terminal that launches it
```

Drop `--dry-run` when you trust it.

## What is not built

- **EQ on two hands** — needs the plug-in page open and a second hand tracked reliably; the address map
  is known, the wiring is not done.
- **Scrub** — routed to MCU jog rather than OSC; the rate limiting is not tuned.
- **Wearables** (Genki Wave ring, Leap Motion 2, Mudra EMG) — the source seam exists
  (`sources/`, one UDP contract) but nobody has the hardware yet.
- **Phase 4 model fallback** ships as a subprocess seam (`gestured --model CMD`), out-of-band on its own
  thread with a hard timeout so a slow answer can never stall the hand. No model is wired to it yet.
