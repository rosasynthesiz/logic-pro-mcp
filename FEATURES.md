# Feature matrix — verified live on Logic Pro 12.3 (Creator Studio build), 2026-09-03

Channel key: **MCU** = Mackie Control emulation (auto-installed via handshake) · **AX** = Accessibility · **MENU** = menu bar via AX · **KEY** = CGEvent key command · **AS** = AppleScript · **MIDI** = Logic Virtual In
Status: ✅ confirmed with readback · 🟡 works, readback partial/uncertain · ⬜ not built yet · ⛔ nobody has solved this

## Done
| Area | Feature | Channel | Status |
|---|---|---|---|
| system | health/doctor with fix plan, permissions, midi_ports, key_map, app, windows | – | ✅ |
| system | ax_dump / ax_find / menu / press_menu (agent can explore Logic's UI + menus) | AX | ✅ reads · 🟡 press_menu is accepted, its effect is not read back |
| transport | play, record | AX press → MCU LED readback | ✅ |
| transport | stop | MCU → KEY fallback | ✅ |
| transport | go_to_beginning | AX button | ✅ |
| transport | set_tempo | AX slider (settable) | ✅ |
| transport | goto bar/beat/div/tick | MENU Navigate > Go To > Position… + typed digits | ✅ |
| transport | toggle_cycle, toggle_count_in | AX checkbox | ✅ |
| transport | toggle_metronome, rewind, fast_forward | MCU / KEY | 🟡 no readback |
| transport | state (playing, recording, tempo, position, cycle, time/key sig, LCD, faders) | MCU + AX | ✅ |
| tracks | list (index, name, mute/solo/arm/monitor, volume dB, pan, selected, folder) | AX | ✅ |
| tracks | select | MCU select (bank-aware) → AX readback | ✅ |
| tracks | mute, solo, arm | MCU strip → AX readback (AX press fallback) | ✅ |
| tracks | monitor | AX checkbox | 🟡 |
| tracks | set_volume (dB) | MCU fader + binary search on Logic's dB text | ✅ ±0.25 dB |
| tracks | set_pan | OSC `/2/pan<strip>` when the track is on the surface bank → Logic's own echo; AX increment/decrement nudging otherwise | ✅ over OSC · 🟡 lands within ~4 on the AX fallback |
| tracks | create_instrument / create_audio / create_external_midi | MENU Track > New … | ✅ |
| tracks | create_drummer (Session Player) | MENU (opens dialog) | 🟡 |
| tracks | rename | MENU "Rename Track" (fresh title guard) + typed name | ✅ |
| tracks | delete | MENU "Delete Track" — refuses unless Logic itself says exactly 1 track | ✅ fail-closed |
| tracks | set_color / get_color (96-swatch palette by index, name, or hex) | MENU Track > Assign Track Color… + real HID mouse click on the swatch; readback = white selection ring via screen capture; guard = Logic's own fresh "Delete Track" count | ✅ |
| tracks | list colors=true (header icon tint → nearest hue) | screen capture of the icon band | 🟡 approximate, None when covered |
| system | session check (locked screen / sleeping display) in doctor + tool errors | Quartz | ✅ |
| midi | note, chord, sequence, cc, program, pitch_bend, aftertouch, sysex | MIDI | 🟡 accepted: sent, nothing reads it back |
| midi | mmc play/stop/record/locate | MIDI | 🟡 needs "Listen to MMC" in Logic |
| midi | mcu_button, mcu_strip, mcu_fader, bank switching | MCU | ✅ mcu_button when that button's LED echoes after the press · 🟡 strip/fader accepted |
| project | launch, activate, quit, info | AS | ✅ launch/quit confirmed by the process appearing/exiting, else accepted |
| project | open | AS | 🟡 |
| project | save, save_as, new, close, undo, redo | KEY | 🟡 dialog may open |
| readback | logic://health, logic://transport, logic://tracks, logic://app | – | ✅ |
| ux | focus returns to the human's previous app after every key send | AS | ✅ |

## Discovered mechanics (worth remembering)
- Logic broadcasts Mackie Control device queries to every new MIDI port; answering `14 01` + `14 14` makes Logic auto-install the surface. ~1.5 s settle before buttons work. Fixed CoreMIDI unique IDs keep the binding across restarts.
- MCU fader 12443/16383 = +0.0 dB. Strip notes: rec 0x00+, solo 0x08+, mute 0x10+, select 0x18+.
- Track/Edit menu titles ("Delete 8 Tracks", "Undo …") only re-validate when the menu opens → open it via AXPress, read, then press or cancel. Also a free forensic tool: fresh "Undo <action>" tells you the last thing that happened in Logic.
- Control-bar playhead sliders, track volume/pan sliders and Go To Position segments only support AXIncrement/AXDecrement; Tempo accepts AXValue.
- "Has Focus" on a track header = Logic's selected track. Tracks scrolled off-screen are absent from the AX header.
- Track color is invisible to AX (both reference repos gave up here). The palette window 'Color' has 97 children: index 0 = reset, 1–96 swatches (4×24, child order 73–96 = top row, 1–24 = row 2, 25–48 = row 3, 49–72 = bottom), only AXPosition resolves, AXPosition.y is the bottom edge. **AXPress and pid-posted clicks are inert on swatches; only a real HID click at (x+12, y-9) works.** Readback: the selected swatch has a 1px white ring → pixel-sample it. The track header paints the color as the icon tile.
- `CGGetDisplaysWithRect` as the first CoreGraphics call in a process aborts with `CGS_REQUIRE_INIT`; call `CGMainDisplayID()` first. `CGDisplayPixelsWide` reports points on this Retina display, so derive capture scale from image width / rect width.
- Locked screen / sleeping display: AXWindows returns the app element itself, System Events sees 0 windows, screen captures are white. Check `CGSessionCopyCurrentDictionary()['CGSSessionScreenIsLocked']` before blaming Logic.

## Next
| Area | Feature | Plan |
|---|---|---|
| mixer | insert_plugin, bypass_plugin, set_send, set_input/output, EQ | open Mixer (AX toggle), map channel-strip slots (AX popups) |
| plugins | inventory, set_param | plugin window AX controls; Scripter insert for verified writes |
| midi | step_input / record_sequence (arm + play + notes → region) | MIDI + MCU record, verify region via AX Regions |
| midi | import .mid | File > Import (MENU) + Open dialog driving |
| edit | split/join/quantize/bounce_in_place/normalize | MENU Edit / Functions |
| navigate | markers (create/goto/rename/delete), zoom, view toggles | MENU Navigate + AX control-bar checkboxes |
| project | bounce / export (File > Bounce, File > Export > … dialogs) | MENU + AX dialog driving |
| audio | analyze exported file, spectrum, EQ suggestions | numpy/scipy on bounced files |
| readback | logic://mixer, logic://regions, logic://markers | AX |
| ⛔ | automation curves, region MIDI note data, arbitrary plugin param readback | not reachable through any channel yet |

## Session 2026-09-03 (later) — probe results

Proven live, not yet wrapped as tools (see ROADMAP.md for the plan):

| Area | Finding |
|---|---|
| MIDI recording | arm via MCU strip + MCU record + send chords + stop creates a real region; Logic's undo history reads `Undo MIDI Recording` |
| Regions | readable per track: `AXLayoutArea` "Track N “Name”" > `AXLayoutItem` children with name, state, screen rect |
| Mixer strip | `volume fader` 0-233 with dB readout, **`pan` -64..+63**, `input gain` -12..+12, `peak level meter` (clipping status only, no number) |
| Plugin params | named `AXSlider`s with `AXValueDescription` in real units; AXValue nudges 1 unit/call, AXIncrement moves 10 |
| Plugin insert | `Mix > Search and Add Plug-in…` dialog is fully AX-readable (search field + result rows) |
| Bounce | Uncompressed + MP3 + M4A checkboxes can be ticked together — one pass, multiple formats |
| Export stems | `File > Export > All Tracks as Audio Files…` → standard macOS panel |
| Global tracks | Arrangement, Marker, Movie, Signature, **Chord**, Tempo, Beat Mapping all exist |
| Movie scoring | `File > Movie > Open Movie…`, `Navigate > Other > Create Movie Scene Cut Markers` |
| Alerts | `AXWindow desc='alert'` with `action-button-1` (confirm) / `action-button-2` (cancel); delete-track-with-regions raises one |
| Gotcha | each process has its own MIDI bus — MCU transport state is per-process, always read it through the server |

## Export chain (built 2026-09-03)

| Step | How | Status |
|---|---|---|
| Bounce dialog | `File > Bounce > Project or Section…`; format checkboxes by AXDescription; per-format options behind the selected table row; popups matched by their visible label, not `_NS:` ids | ✅ |
| Bar range | Start/End segmented fields — click, type digits, Return. Logic recalculates "Requires N MB (Time m:ss)" as proof the range took | ✅ |
| Save panel | **runs in `com.apple.appkit.xpc.openAndSavePanelService`, a separate process.** Reads and AXValue writes are proxied fine, but AXPress on its buttons fails and keys posted to Logic's pid go nowhere. Drive with HID input: `keys.press(..., pid=None)` and a real mouse click, after activating Logic | ✅ |
| Destination | `Cmd+Shift+G`, type path, Return; the Go-to-Folder sheet's field is `PathTextField`; verify via the `where popup` value | ✅ |
| Completion | poll for the expected files, require a changed mtime and a size that stops growing | ✅ |
| Verified | WAV + MP3 in one 17 s pass, sizes correct, no leftover dialogs | ✅ |

## Plugin engine (2026-09-03)

| Feature | Channel | Status |
|---|---|---|
| insert plugin by name | MENU `Mix > Search and Add Plug-in…` | ✅ |
| describe all parameters/switches/menus with real-unit readouts | AX | ✅ |
| get / set parameter to a target value (dB, Hz, %, ratio) | AX step + readout verify | ✅ on well-labelled plugins |
| toggle switches (EQ bands, Analyzer, HQ) | AX | ✅ |
| disabled-band detection + auto-enable | AX `AXEnabled` | ✅ |
| ambiguous parameter names refused, never guessed | – | ✅ fail-closed |
| plugins that leave sliders unnamed (Compressor) | AX + positional label matching | 🟡 fuzzy, flagged `labelled_by: position` |

Mechanics: `AXValue` nudges 1 internal unit per call regardless of the value passed; `AXIncrement`/`AXDecrement`
move 10; internal→real mapping is non-linear, so the setter steps, re-reads the plugin's readout, measures the
local step size, and converges. A realistic vocal EQ (90 Hz cut, −2.6 dB at 390 Hz, +2 dB at 5.75 kHz,
+1.7 dB shelf) applied and verified in one pass.

Scratch project for this work: `~/Music/Logic/logic-mcp scratch.logicx`.

## Auto-leveling (2026-09-03)

| Feature | How | Status |
|---|---|---|
| Integrated loudness (LUFS) | BS.1770-4 K-weighting, 400 ms/75% blocks, absolute + relative gate | ✅ |
| True peak | 4x FFT oversampling | ✅ |
| Peak, RMS, crest factor, stereo correlation, 7-band spectrum | numpy | ✅ |
| Role detection from stem names | regex table (vox/drums/bass/gtr/keys/pad/fx…) | ✅ |
| Gain plan relative to a reference stem | role targets in LU, headroom guard, global down-shift if anything wants a boost | ✅ |
| Pan plan | role spread, stems already wide (correlation < 0.6) left alone | ✅ |
| Apply gains to tracks | match stem to track by name, relative move on the current fader, dB readback | ✅ ±0.2 dB |
| Apply pans | header pan slider | 🟡 settles within ~4 of 127, reported `uncertain` |
| dry_run | – | ✅ |

Speed: 5 stems × 460 s analysed in 5.8 s (scipy `lfilter` for the K-weighting; cumulative sums for the block
loudness — the naive Python biquad took 107 s). Apply took 14 s.

Bugs the live test caught: `\bdrum\b` never matched "Drums" so it fell through to the generic role; and the
reference stem was being asked to move itself. Both fixed.

## Review fixes, round 2 (2026-09-04) — plugin engine

| Bug | Fix |
|---|---|
| `4:1` ratio readouts unparseable, so Compressor Ratio could be neither read nor set | dedicated ratio pattern → `(4.0, ':1')` |
| `+∞ dB` parsed as **negative** infinity | sign read from the string |
| Any window that was not the arrange window counted as a plugin editor — including the Mixer | structural test: a `bypass` control plus a `close` button (bypass is a checkbox on Channel EQ, a button on the Compressor) |
| Setter crashed with TypeError when a readout became unreadable mid-loop | every `_read` result guarded; returns `settle()` instead |
| Stall detector used an absolute 0.5 raw-unit threshold, so normalised 0..1 sliders always looked stalled | threshold derived from the control's own min/max span |
| Blind coarse burst could be thousands of steps when the readout barely moved, pinning the control at its limit | burst bounded by `max_burst` (12) before re-measuring |
| An infinite target returned `ok` without moving anything | non-finite targets refused |
| Target unit was parsed then discarded, so `"250 Hz"` into a percent control drove it anywhere | unit compared against the control's own readout, mismatch refused |
| `find_control` returned None for both "not found" and "ambiguous" | `resolve_control` returns a reason; the tool says which |
| `enable_band` switched an EQ band on and left it on when the write then failed | rolled back on failure |

## Real-use testing (2026-09-04) — what a 24-track project exposed

Everything up to here was proven on a 6-track scratch project. Scaling up found a serious defect.

| Check | Result |
|---|---|
| Do track headers stay visible to AX when scrolled off? | ✅ all 24 readable, scrolling is not a limit |
| Opening a project while another has unsaved changes | ⚠️ raises a save/don't-save alert that blocks everything — `dialogs.answer_alert` handles it, but callers must expect it |
| Operating on tracks past the first 8 | ❌ **wrote to the wrong tracks** — now fixed |
| MCU bank switching (`bank_right`, `channel_right`) | ❌ **does not work at all** on this setup: the LCD never changes |

**The wrong-track bug.** `strip_for()` dead-reckoned the MCU bank as `divmod(index-1, 8)` and assumed the
bank buttons had moved the surface. They had not. So a fader write aimed at track 11 landed on whatever track
was sitting on strip 2 of bank 0 — track 3. Measured: asking for tracks 11, 19 and 24 also silently moved
tracks 3, 8 and 16 to -6.2 dB. On a real mix that quietly destroys someone's balance, and nothing in the
result would have hinted at it.

**Fix.** The bank is now *read* from the MCU LCD rather than assumed. `bank_offset()` reconciles the eight
displayed strip names against the track list (Logic abbreviates them, so names match as a subsequence), and
`strip_for()` returns `None` whenever the requested track is not provably on the surface. Callers fall back
to Accessibility, and `nudge_slider_to_db` drives the fader against its own dB readout. Verified: strips
resolve for tracks 1-8, return `None` for 9+, and every track past 8 is now set correctly through AX.

## Working on a real 131-track session (2026-09-04)

| Finding | Detail |
|---|---|
| Track headers scroll far outside the viewport | Track 4 sat at y = **-751**. Clicks there land nowhere. |
| `AXPress` on "Has Focus" is inert | Reports success, selection does not change |
| Writing `AXSelected` is inert for off-screen rows | `AXUIElementSetAttributeValue` returns 0 (success) and nothing happens |
| The only reliable selection | scroll the row into view via the scrollbar, real HID click, then read the selection back |
| The header's own rect is the scrolling **content**, not the viewport | its y goes negative; the viewport comes from the scrollbar's rect |
| Sample-library vendor prefixes look like instruments | `VOX_LCH_128_bass_synth…` is a *publisher* code — it was about to file a bass, a trumpet and a guitar under vocals |

`scroll_track_into_view()` computes the scrollbar fraction directly (travel is linear, ~1086 pt per unit) and
verifies; `click_select_track()` builds on it and confirms the selection is exactly the intended track before
any write. Colouring 110 tracks: 92 first pass, 12 on retry, remainder swept by a verify pass that skips rows
already correct.

## 2026-09-09 — test suite, dialogue detector, end-user delivery, missing resources

### Test suite (new — there were none)
`uv run pytest`, 157+ tests, all running with **Logic closed and no Accessibility grant**. That constraint is
the point: the modules under test are the ones that decide what is true, so they have to be checkable on any
machine and in CI. Anything needing the app is marked `live` and excluded by default.

Synthetic audio is generated in `tests/conftest.py` rather than committed, and building it taught something:
a "speech-like" fixture made of band-filtered **noise** has no pitch at all, so the strict music gate threw
it out as music. Real speech is voiced with a gliding f0, and the fixture now models that (phase-integrated
harmonic source, ~4 Hz syllabic gating, consonant noise at onsets). The test found the bad fixture, not a bad
gate — which is what these are for.

`tests/test_projectdata.py` builds a synthetic ProjectData **independently from the documented layout** and
checks the parser against it, so the builder is a second statement of the format; if the two ever disagree,
that disagreement is itself the finding.

### Dialogue detector — both known defects fixed, measured
Baseline on the real material (episode 03 bounce + three instrumental-only files): 119 cues, longest 13.99 s,
**40 false positives on music**.

* **Over-merging.** `max_duration` (default 12 s) splits any long cue at its least speech-like interior
  frame — the natural pause the hysteresis bridged over — so both halves stay whole utterances. A split
  never manufactures a cue below `min_speech`, and a segment with no legal interior cut is left long rather
  than chopped arbitrarily. Verified bounding: 12 → 11.86 s, 6 → 5.74 s, 3 → 3.00 s, with total speech time
  preserved.
* **Music false positives.** Candidate discriminators were measured before anything was gated:

  | feature | dialogue median | music median | verdict |
  |---|---|---|---|
  | low_energy_ratio | 0.233 | 0.237 | **no separation** — the textbook feature, useless here |
  | zcr_std | 0.063 | 0.052 | weak, overlapping |
  | flux_var | 0.0071 | 0.0029 | **2.4×, the only clean single discriminator** |
  | f0_hold | 0.585 | 0.297 / 1.000 | two-sided only (percussion low, held notes high) |

  So the default gate uses `flux_var` alone: **music false positives 40 → 23 (−42 %) for −4 % of dialogue**.
  Adding an f0_hold cap cost a further 5 % of dialogue and removed **no** extra music, so it lives only in
  `music_gate="strict"` (40 → 13, −67 %, at −14 % of dialogue). Rejected segments stay in the payload with
  their reason and features, so a wrong call is auditable rather than invisible.

  Bias is deliberately toward recall: a spurious marker is one click to delete, a missing one is a missed
  line. Thresholds come from four files — thin. Re-measure before tightening.

### Delivery now uses the end user's own account
No credentials and no default remote ship with the server. The remote comes from the argument, then
`$LOGIC_MCP_REMOTE`, then the single configured remote; with several configured and none pinned it **refuses
rather than guessing** — picking wrong uploads unreleased audio to the wrong account, and that is not
undoable. `logic_deliver(action="setup")` returns the commands for the user to run themselves; the sign-in
happens in their browser under their own account and this process never sees a token.

### New resources
| Resource | Notes |
|---|---|
| `logic://markers` | marker text comes from AXDescription, not AXValue |
| `logic://mixer` | the 8 strips **currently** on the OSC surface, read from Logic's feedback — never a dead-reckoned bank |
| `logic://regions` | offscreen regions are listed with `visible: false` and no geometry rather than dropped, so "none" and "can't see from here" stay distinguishable. Screen rectangles only — bar positions need the ruler's zoom and scroll, and guessing them yields plausible numbers that are untrue |

### Pan
Now routed over OSC (`/2/pan<strip>`) when the track is provably on the surface bank, where the echo is
Logic's own value and the result can honestly be `confirmed`. Strip resolution reads the bank from feedback
and returns `None` when the track is not on it — the same fail-closed rule the MCU path learned after
dead reckoning silently set three wrong tracks to −6.2 dB.

### Known limit, unchanged: detector memory
`detect_cues` peaks around 2 GB of RSS on a 10-minute file. That is the framed-FFT pipeline in
`_frame_features` (complex128 spectra over ~60 000 frames), not the new gate — retaining the framed signal
was ruled out as the cause by measurement. It is fine for episode-length programmes and would need chunked
framing for feature-length material; `max_seconds` is the mitigation in the meantime.

### Bugs the new test suite found on its first run
Writing tests against code nobody had tested surfaced four real defects. All are fixed:

| Where | Defect | Fix |
|---|---|---|
| `leveling.py` | the docstring and README both said "the reference stem does not move", but the global downward shift moved it too (reference at −14 LUFS beside a guitar wanting +4 dB came back at **−4.0 dB**, not 0) | behaviour is correct — it is what preserves the balance — so the **documentation** was wrong. Corrected, and the plan now reports `global_shift_db` and `reference.moved_by_db` explicitly |
| `mix.py` | `parse()` / `recipe_for()` read a hand-written 8-entry `RECIPES` dict, shadowing the real 34-recipe `mix_recipes.json`. **Zero callers anywhere.** Its term matching had no word boundary, so `"do something"` resolved to the `thin` recipe via the substring inside `"something"` | deleted, along with `RECIPES`/`SYNONYMS`. A test keeps it deleted — dead code that reads like live code is a trap |
| `instruments.py` | `hint(name, family)` accepted a `family` it never read, implying a family-awareness it has never had | parameter removed |
| `tests/conftest.py` | the first "speech-like" fixture was band-filtered **noise**, which has no pitch, so the strict music gate correctly threw it out | fixture rewritten as a voiced source with a gliding f0 — the test found a bad fixture, not a bad gate |


## Gesture and voice control (2026-09-20)

Phases 0-3 and 5 of `GESTURE_VOICE_PLAN.md`, built on the existing channels. Full write-up in
`docs/gesture.md`. 4 processes, ~500 tests across the stack, none of which needs Logic.

| Area | Feature | Channel | Status |
|---|---|---|---|
| padd | `logicpadd` owns the one OSC socket + Bonjour advert, serves it over a unix socket | – | ✅ 20 tests |
| padd | `RemoteLogicPad` duck-types `LogicPad`; `acquire()` falls back to direct when no daemon | – | ✅ |
| padd | `forget()` deliberately not exposed (withdrawing the advert is the measured SIGSEGV) | – | ✅ pinned by a test |
| server | `_osc_ready` prefers the daemon, unchanged behaviour when none runs | – | ✅ |
| arbiter | IDLE / ARMED / ENGAGED / FROZEN, pure state machine, no IO | – | ✅ 33 tests |
| arbiter | no target = no motion; poses suppressed while engaged; release freezes, never resets | – | ✅ |
| hands | `handd` Swift: AVCapture + Vision, 21 joints, 60 fps, UDP JSON | Vision | ✅ builds |
| hands | `PoseClassifier`: 6 poses, span-normalised, hysteresis + dwell | – | ✅ 42 tests |
| hands | One Euro filter, calibration box, dB/pan/Hz curves + inverses, soft takeover | – | ✅ |
| voice | `voiced`: pedal push-to-talk (CC 64), parakeet/whisper.cpp behind an ABC, `--dry-run` | MIDI | 🟡 needs the `voice` extra installed |
| voice | grammar: ~40 commands, spoken numbers, ASR-mishearing table, fail-closed on ambiguity | – | ✅ 178 tests |
| axes | volume `/1/volume<n>`, pan `/2/pan<n>`, plug-in param `/3/par<n>` — one datagram, no readback | OSC | ✅ |
| axes | scrub (MCU jog), EQ two-hand | – | ⬜ address map known, not wired |
| regions | `LiveDrag` + trim one edge, verified by re-reading BOTH edges, auto-⌘Z if the body moved | AX + HID | 🟡 untested against live Logic |
| hud | always-on-top NSPanel fed from Logic's own echo | Cocoa | ✅ |
| phase 4 | model fallback as an out-of-band subprocess seam with a hard timeout | – | 🟡 seam only |
| phase 6 | Genki Wave / Leap 2 / Mudra | – | ⬜ no hardware |

### Defects the integration found (all fixed)
- `UdpReceiver.drain()` looped until the socket went quiet for its timeout. A 60 fps camera never goes
  quiet for 50 ms, so it only returned on scheduler jitter: poses never settled long enough to classify
  and the HUD updated at random. Now reads non-blocking with a hard cap.
- `Arbiter.on_hands_lost` cleared `self.pose` before delegating to `on_pose`, which short-circuits when
  the pose has not changed — so losing the hand was a silent no-op and the ride stayed engaged.
- A pose arriving in the same transition as a clutch release fired as a command. Fingers uncurling out
  of a pinch read as a fist for one frame, so every long ride ended by stopping the transport.
- The synthetic fist curled the thumb toward the wrist; a real thumb folds *across* the palm toward the
  pinky, which is what the classifier measures. No fist ever classified.
- `PaddServer.stop()` used `conn.close()`, but `socket.makefile()` on the handler thread holds its own
  reference, so a restarted daemon's clients kept talking to the dead instance. Needs `shutdown()` first.
- Fixed test ports raced between overlapping suite runs; the kernel picks them now.

### Live test against Logic (2026-09-20)

Run on `~/Music/Logic/logic-mcp scratch.logicx`, Logic Pro 12.3 Creator Studio, real OSC surface via
`logicpadd`. Not simulated.

| Step | Result |
|---|---|
| `logicpadd` binds 9000, adopts the advert, Logic already had the device | ✅ 118 feedback messages, real bank read back (`Audio 13`..`Audio 20`) |
| "volume on audio 15" typed into `voiced --dry-run` | ✅ ARMED on strip 3 by name |
| pinch + hand high | ✅ Logic's fader 0.7087 → **1.0000** |
| hand low | ✅ → **0.0771** |
| release with the hand at the floor | ✅ **held at 0.0771** — rule 3 holds on real hardware |
| open palm | ✅ Logic `play=1.0` |
| fist | ✅ Logic `stop=1.0` |
| fader restored afterwards | ✅ back to 0.7087 |

Still unproven against Logic: region trim, scrub, EQ, the real camera (`handd` has never run), and the
pedal trigger.

### Defect found during the live test

`target.running()` used `NSWorkspace.sharedWorkspace().runningApplications()`, whose list is seeded once
and then kept current by notifications delivered on a Cocoa run loop. A stdio MCP server has no run
loop, so its snapshot freezes at start-up: **a Logic launched after the server is invisible to it
forever**, and every tool reports "Logic not running" while Logic sits there on screen. Now falls back
to walking `ps`, which has no such memory.

## 2026-10-08 — publish prep (the three safety items from the 09-11 eval)

| Defect | Fix | Test |
|---|---|---|
| AppleScript injection: names and paths dropped straight into `"…"` literals | `applescript._q` escapes `\` and `"`; applied to every literal | `tests/test_applescript.py` — a hostile file name cannot leave the literal |
| OSC echo cache accepted datagrams from any LAN host, so anyone on the Wi-Fi could forge a `confirmed` | `LogicPad._ingest` drops anything not from our own address or loopback; counted in `rejected` | `test_feedback_from_another_host_is_rejected_not_cached` |
| `midi.bank_offset` returned the first offset whose window matched and checked only as many LCD cells as the window had — short tails matched full LCDs, repeated names picked the first block, and a write then hit the wrong track | all eight cells must agree (blank cell ⇔ no track) and the answer must be unique; otherwise None and the caller falls back to AX | `tests/test_midi_bank.py` — the eval's three wrong answers, plus repeated names |

Also: MIT `LICENSE`, `[project.urls]`, author email removed, `Sessions/` ignored, private project names scrubbed from code,
fixtures and tests. `HANDOVER.md` stays out of the public copy. Public home: `github.com/rosasynthesiz/logic-mcp`,
published as a single-commit snapshot — the full history stays in the private repo.

## 2026-10-06 — the unverified items, tested live

| Item | Result |
|---|---|
| whisper.cpp voice backend | ✅ `say` → 16 kHz wav → `whisper-cli` (ggml-base.en) → grammar: "volume on audio fifteen" → `volume / audio 15`, "mute the drums" → `mute / drums`, "select track four" → `select / 4`. 3/3 |
| sustain-pedal trigger | ✅ virtual CoreMIDI source "FakePedal": CC64 = 127 / 0 / 100 / 30 → open / close / open / close (threshold 64) |
| phase-4 model fallback seam | ✅ an unparseable line with `--model CMD` → the command's JSON → `armed`; the same line without `--model` → `unparsed` |
| region trim against live Logic | ✅ 1-bar MIDI region: asked −30 px got −24 (Smart snap, one beat), −48 → −48, +48 → +48; the untouched edge checked each time; ⌘Z restored the width each time |
| real camera → real fader | ✅ `handd` launched from Terminal.app (the camera grant follows the launching app; a shell under Claude is denied). Ride 1: Drums −3.5 dB → −∞, release **held**. Ride 2, read from Logic's echo: 0.579 → 0.479 → 0.379 → … → 0.022 in 0.7 s — the catch takeover's fixed step, never a jump — held until the FROZEN timeout. Both rides went down only: the hand was below the fader at engage |
| scrub, EQ on two hands | ⬜ nothing wired, nothing to test |

### Defects found
| Where | Defect | Status |
|---|---|---|
| `ax_logic.tracks_area` | took the first `AXLayoutArea` in the window, which with the inspector open is the channel-strip **Mixer** — `logic://regions` listed 'Read, automation enabled' and the patch name as regions and missed every real one | fixed: finds the `Tracks contents` group; each lane is its own `AXLayoutArea` |
| `regions.trim` | one −30 px request came back **+96 px** (one bar) and was reported `confirmed` — the guard only checked the other edge | fixed: a move in the wrong direction is `failed` + ⌘Z |
| `regions.trim` | AX reports a region's full extent, so an edge past the display (x=2013 on 1728 pt) was pressed on nothing and reported `uncertain` | fixed: refused up front with "scroll or zoom" |
| `padd.RemoteLogicPad` | lacks `ensure_page`, `page`, `full_refresh`, `strip_view`, `eq_view`, `eq_address`, `set_norm`, `set_target`, `param_address`, `insert_address`, `recent` — **every `logic_osc` action raises while `logicpadd` runs**, which is the one configuration padd exists for | fixed: `RemoteLogicPad` mixes in the same `_Pages` as `LogicPad`; the views read the echo cache through `_state_get`, one daemon round trip per key; `recent` is a daemon op; a parity test lists every method the server calls. Verified live: `logic_osc status` and `strip` (page 3 → page 1) through a running `logicpadd`, fresh process |
| `osc.LogicPad.connected` | the Wi-Fi address changed mid-session (10.179.131.116 → 10.213.26.157). The advert, Logic's device socket and every `send()` stayed pinned to the dead address; `connected` stayed **true** because `lsof` still saw Logic's socket to it. Logic drops packets from any other source, so no workaround short of: quit Logic → kill the advert → restart `logicpadd` → relaunch | fixed (detection): `LogicPad.ip_stale` = our address is no longer on any interface; `connected` is false while it is; `logicpadd status` reports `ip`/`ip_stale`; `_osc_ready` names the dead address and the recovery steps instead of "has not bound". The address changed **twice more** during this session (→ 10.86.237.157) |
| `logic_transport record` | recorded 45 s of silence onto **Audio 21**, which was already armed in the saved scratch project; the new instrument track was not the only armed one | note: read every track's `arm` before recording |
| `applescript.open_file` / `quit_app` | both returned `osascript timeout` although the action happened (Logic needs >10 s to open or to put up its save alert) | open |
| Logic itself | quitting with the scratch project open (Don't Save) ended in `EXC_BREAKPOINT` in `CGImageRetain` on Logic's main thread (`DiagnosticReports/…2026-10-05-195255.ips`), not the Bonjour `SIGSEGV`. Logic's own; not reproduced | note |
| shared surface | `strip_view` / `eq_view` leave the surface on page 3 / 4, and Logic drops `/1/*` while it is there — a `logic_osc strip` call during a hand ride would silently stop the fader. `gestured` never asserts page 1 | open: either padd owns the page or `gestured` calls `ensure_page(1)` before each write |

### Third ride (home Wi-Fi, 192.168.1.27) — both directions, recorded from Logic's echo

Three engages in one ARMED window. Drums started at 0.579 (−3.5 dB):

| t (s) | what the fader did |
|---|---|
| 27.0 → 27.3 | engage with the hand low: 0.579 → 0.479 → 0.329 → 0.229 → 0.079 → **0.000** in 0.33 s |
| 30.9 → 32.0 | re-engage: 0.000 → **0.102** (up), then followed the hand down to 0.000 |
| 33.3 → 34.7 | re-engage with the hand "high": 0.050 → 0.100 → **0.196**, settled 0.173, released and **held 0.168** for 3½ min until restored |

Both directions move and the release holds — rule 3 again. Two things the numbers say that the plan did not:
- **The default calibration box is wrong for this desk.** A hand held above the chest reads ≤ 0.2; every ride
  parks near the floor. `docs/gesture.md` already says the default box is a starting point, not a setting;
  this is what "not a setting" looks like. Calibrate before the next session.
- **"Catch" takeover is a 0.3 s slam, not a catch.** 0.1–0.15 per echo at loop rate crosses the whole fader in a
  third of a second — technically never a jump, practically indistinguishable from one. `CATCH_MAX_STEP` is
  sized per call, and the loop runs at 120 Hz; it wants to be sized per second.

Both fixed the same night (`CATCH_RATE` per second; `gestured --calibrate` / `--box`), then a fourth ride:

| | |
|---|---|
| `gestured --calibrate 30` | wrist covered **x 0.68–0.95, y 0.58–1.00** — the hands live in the bottom-right of a MacBook frame; the default middle box (y 0.15–0.85) put "all the way up" at 0.4 and the desk below 0 |
| ride with `--box 0.676,0.582,0.948,1.0` | 0.579 → 0.571 → 0.554 → 0.538 → 0.521 → 0.504 over ~1 s, then back up to 0.538, released and **held**. Steps are now 0.017 per echo (≈ `CATCH_RATE` / Logic's echo rate) — a catch, not a slam |
| still unknown | whether a pinch at the top of the box drives the fader to 1.0: this ride's hand sat mid-box the whole time. Next time record the HUD stream (UDP 9103, the hand's mapped value) next to Logic's echo so the two can be compared |
| **2026-10-07, full range** | Stable address at last. With the HUD open (`gesture-hud` — the answer to "I can't see the camera, am I even pinching?"; it shows POSE and STATE live, and the pinch that would not register all morning registered on the first try once she could see it) Drums rode **0.00 ↔ 1.00 three times over** in 300 s — 1,044 echoes, slopes ≈ 0.27/s set by the hand, not the limiter — and released at **0.922 (+4.5 dB), held**. Up, down, hold, all from a real hand on the calibrated box. Also learned: Logic that is open with **no** device bound picks up a fresh advert live, no relaunch — the relaunch is only needed to move an already-bound device |
| the address moved again | four minutes after the ride, on home Wi-Fi: 192.168.1.27 → .31, the **fifth** address of the evening. The new check caught it (`logicpadd status`: `connected false, ip_stale true`) where the old one would have said connected; the fader was restored over AX instead. Five leases in one evening is a router problem — give this Mac a DHCP reservation — but the detection is what turns a silent failure into a named one |
