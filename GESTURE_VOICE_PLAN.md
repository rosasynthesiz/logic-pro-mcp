# Gesture + voice control for Logic — research and plan

Date: 2026-09-20. Target: drive Logic Pro 12.3 (Creator Studio) by hand and by voice, on top of the
channels `logic-mcp` already proved (OSC, MCU, AX, CGEvent keys, HID mouse/drag, MIDI).

## 1. The thesis

Everything that has ever failed at "control your DAW with gestures" failed the same way: it tried to make
the hand do the naming. A DAW has ~1000 commands and ~100 tracks; a hand has maybe six poses you can hit
reliably without looking. So don't name with the hand.

> **Voice carries the nouns and the verbs. Gesture carries the value.**

"Volume on strings" — now your hand *is* that fader until you let go. This is Bolt's 1980 *Put-That-There*
split, and it's the only multimodal arrangement that has ever survived contact with real work.

It maps cleanly onto the two things the repo already has:

| Channel | Traffic | Why |
|---|---|---|
| OSC over UDP (`channels/osc.py`) | continuous values, 60/s | one socket, sub-millisecond, Logic echoes back |
| MCU / AX / keys / HID drag | discrete commands, ~1/s | slower, but each one returns `confirmed` |

And it dictates the hard rule of the whole design:

> **The model is never in the gesture loop.** A fader ride at 60 Hz through an LLM is 1-3 s per sample.
> The gesture daemon writes OSC directly. The model only ever appears on the voice path, and only when the
> fast grammar misses.

## 2. Latency budget (why this is the split)

| Path | Stages | Total |
|---|---|---|
| Gesture → fader | camera 16 ms + hand pose 8 ms + smoothing 20 ms + OSC <1 ms | **~45 ms** |
| Gesture → fader (ring/EMG) | BLE 10 ms + map 1 ms + OSC <1 ms | **~12 ms** |
| Voice → discrete cmd (grammar hit) | speech 400 ms + regex 1 ms + AX/MCU 150 ms | **~550 ms** |
| Voice → discrete cmd (model fallback) | speech 400 ms + MCP round trip 1-3 s | **~2 s** |

Mixing tolerates ~50 ms on a fader. Playing an instrument does not — so anything performance-critical goes
on a wearable, not a camera.

## 3. Sensing options — what's actually available on this Mac

MacBook Pro M5 Max, 36 GB, built-in camera only. `superwhisper.app` already installed.

| Option | Cost | Latency | DOF | Fatigue | Verdict |
|---|---|---|---|---|---|
| **Built-in camera + Apple Vision framework** (`VNDetectHumanHandPoseRequest`) | £0 | ~45 ms | 21 joints, 2D + weak depth | high — arms up in front of the screen | **Phase 1.** Zero hardware, zero deps, ships on the OS, 60 fps on Apple Silicon. |
| **MediaPipe Hands** (Python) | £0 | ~50 ms | 21 joints, 2.5D | same | Only if Vision's classifier disappoints. Extra dep, not obviously better on Apple Silicon. |
| **iPhone as a second camera** (Continuity or a small streaming app) | £0 | ~60 ms | better geometry | **low** — put it on a stand looking *down at the desk*, hands rest at desk level | Cheap fix for gorilla arm. Best free ergonomics. |
| **Ultraleap Leap Motion Controller 2** | ~£140 | ~10 ms | 27 elements, true 3D, occlusion-robust | low (desk-mounted, looking up) | **Best camera option.** Gemini has supported Apple Silicon since Aug 2023. |
| **Genki Wave ring** | ~£250 | ~10 ms | tilt, pan, roll, vibrato, tap, click | none — works with hands on the keys | **Best performance option.** Speaks BLE MIDI natively, so Logic MIDI-learns it with *no code at all*. |
| **Mudra Link EMG wristband** | ~£200 | ~20 ms | finger micro-gestures, pinch pressure | none, no line of sight needed | Interesting dark horse: exposes a local stream at `ws://127.0.0.1:8766`, Python SDK on Mudra Pro. Works in the dark, hands at rest. |
| **MiMU gloves** | ~£2500 | low | very high | medium | Out of scope. |

**Recommendation:** Phase 1 on the built-in camera to prove the loop costs nothing. If it earns its keep,
buy a **Genki Wave** (continuous control while playing) and/or a **Leap 2** (full hand vocabulary at the desk).
They're complementary, not competing.

## 4. Voice options

| Option | Latency | Notes |
|---|---|---|
| **superwhisper** (installed) | ~400 ms local, Parakeet/whisper-turbo | No programmable extension points — it types into the focused field. Usable as a dumb dictation source, not as an event bus. |
| **Own `parakeet-mlx` / `whisper.cpp` daemon** | ~300 ms | The right answer: we need the *text as an event*, not as keystrokes. Streams into our own parser. |
| Apple Speech framework | ~300 ms | On-device, free, but weaker on accented English and useless for Tamil. |

Tamil/Tanglish: see the existing note on `lemuralabs/tamil-asr-qwen3`. Run the command grammar in English
first; a bilingual model only matters once the English path works.

**Push-to-talk beats a wake word.** "Hey Logic" in a room with music playing is a false-trigger machine.
Use a **sustain pedal** — it's already a MIDI device, the repo already reads MIDI, and your hands stay on
the instrument. Hold pedal → mic hot. Release → parse.

## 5. Gesture map (the part that has to be small)

Discrete poses: **six, maximum.** Every one needs a dwell time and hysteresis, or you'll fire commands
while reaching for coffee.

| Gesture | Binds to | Channel |
|---|---|---|
| Open palm → fist | stop | MCU/AX |
| Fist → open palm | play | MCU/AX |
| Swipe left | undo | KEY (⌘Z) |
| Pinch + hold | **the clutch** — engage whatever the current mode is | — |
| Two-hand spread | zoom the arrange window | KEY |
| Point + dwell | select the track under the pointer | MCU select |

Continuous bindings (all live behind the clutch, all over OSC):

| Motion | Binds to | Address |
|---|---|---|
| Hand height | volume of the target track | `/1/volume<N>` |
| Hand roll | pan | `/2/pan<N>` |
| Pinch distance | focused plug-in parameter | `/3/par<N>` |
| Two-hand x/y | EQ freq × gain | `/4/...` |
| Hand x-drag | playhead scrub | AX / MCU |
| Hand x-drag while a region is armed | **region trim** | HID drag on the region's AX rect |

Region trim is the sleeper feature and it's already 90% built: regions are readable per track as
`AXLayoutArea` → `AXLayoutItem` with a real screen rect, and `channels/mouse.py:95 drag()` does a proper
HID drag with the press-pause AppKit needs. Hand x → drag the region's right edge. Nobody else has this.

## 6. The Midas problem, and the clutch

The reason gesture UIs feel cursed is that the hand is always there. Three locks:

1. **Clutch** — nothing moves unless pinched (camera/Leap) or a ring button is held (Genki). Release = freeze
   at the last value, never snap to zero.
2. **Target lock** — the hand controls whatever voice last named. No name, no target, no motion.
3. **Timeout** — the target releases after 4 s of no motion.

Plus: never bind a destructive command to a gesture. Delete, bounce and save stay on voice, and stay on the
existing `confirmed | uncertain | failed` envelope.

## 7. Feedback

You cannot look at your hands and the screen at once. Two cheap channels:

- **Audible** — you're already listening to the fader move. That's most of it.
- **A HUD** — a small always-on-top overlay showing target track + current value + clutch state. Logic already
  streams its own state back over OSC, so the HUD renders Logic's truth, not our intention.

## 8. Build order

| Phase | What | Proves |
|---|---|---|
| 0 | `gestured` skeleton: UDP event bus, arbiter state machine, HUD stub, fake input source | the plumbing, with no sensors |
| 1 | Vision framework hand pose (small Swift helper) → bus. Clutch + hand height → `/1/volume<N>` | the whole loop, end to end, £0 |
| 2 | Local ASR daemon + the ~40-command grammar. Pedal push-to-talk | "mute strings", "solo drums", "select track 4" |
| 3 | Arbiter target lock: voice names, hand values | the actual thesis |
| 4 | Model fallback through `logic-mcp` for anything the grammar misses | "make the chorus louder than the verse" |
| 5 | Region trim by hand (AX rect + HID drag) | the feature nobody else has |
| 6 | Genki Wave and/or Leap 2, same bus, swap the source | performance-grade latency |

Phases 0-3 are the real project. 4-6 are upside.

## 9. Known risks

- Accessibility grants wedge; the fix is restarting **Logic**, not the permission (see project notes).
- Camera hand tracking degrades badly in low studio light — the usual state of a studio.
- Gorilla arm is real. Design for desk level and short bursts, not sustained conducting.
- The MCU surface only ever addresses the 8 strips Logic is currently showing. Targeting beyond the bank
  must go through voice + OSC, never through an assumed bank.
- Everything stays local. No camera or mic audio leaves the machine.
