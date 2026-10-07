# logic-mcp

Own-build MCP server that lets AI agents (Claude Code, Claude Desktop, Hermes, Cursor…) drive **Logic Pro** on macOS.
Works with both the desktop purchase and the **Creator Studio** subscription build (`com.apple.mobilelogic`).

Apple ships no API, so this talks to Logic the way hardware does:

| Channel | What it does | Readback |
|---|---|---|
| **OSC** (a fake TouchOSC control surface) | mixer strips, pan, selection, transport, plug-in parameters, Channel EQ — the best path for anything with a value | Logic echoes its own state back |
| **MCU** (Mackie Control emulation over virtual CoreMIDI) | transport, and per-strip mute/solo/arm/select/faders for the eight tracks Logic currently shows on the surface | LED echo, LCD text, fader echo |
| **Accessibility** | control bar (Play/Record/Cycle/Count In, tempo, playhead), track headers, dialogs | live UI values |
| **CGEvent** key commands | anything with a shortcut (new track, save, undo, editors) | via AX |
| **AppleScript** | launch / activate / open / quit | – |
| **Logic Pro Virtual In** MIDI | notes, chords, sequences, CC, program, pitch bend, SysEx, MMC | – |

Zero manual setup in Logic: Logic auto-scans new MIDI ports with Mackie Control device queries, we answer the
handshake, and Logic installs the surface itself. Endpoints carry fixed CoreMIDI unique IDs so the binding survives restarts.

## Install
```bash
cd "/path/to/logic-mcp"
uv sync
uv run logic-mcp-doctor      # which channels are ready + what to click
```
Permissions (System Settings → Privacy & Security): **Accessibility** and **Automation → System Events** for the
process that runs the server (Claude, Terminal, Hermes…).

### Register
```bash
claude mcp add --scope user logic-mcp -- uv run --directory "/path/to/logic-mcp" logic-mcp
hermes mcp add logic-mcp --command uv --args run --directory "/path/to/logic-mcp" logic-mcp
```

## Tools
- `logic_system` — health/doctor, permissions, midi_ports, key_map, `ax_dump` / `ax_find` (explore Logic's UI tree)
- `logic_transport` — play, stop, record, go_to_beginning, rewind, fast_forward, toggle_cycle, toggle_count_in, toggle_metronome, set_tempo, goto, state
- `logic_tracks` — list (optionally with colors), select, mute, solo, arm, monitor, set_volume (dB), set_pan, set_color / get_color (96-swatch palette: index, name like `dark green`, or hex), palette, create_*, delete, rename
- `logic_midi` — note, chord, sequence, cc, program, pitch_bend, aftertouch, sysex, mmc, mcu_button/strip/fader
- `logic_plugins` — insert by name, describe/get/set parameters in real units (dB, Hz, %), switches, presets
- `logic_level` — measure rendered audio (LUFS, true peak, crest, correlation, spectrum) and balance the mix from it
- `logic_export` — bounce (several formats in one pass), stems, read bounce settings, dismiss stray dialogs
- `logic_deliver` — upload to cloud storage, or the whole chain: bounce, verify, upload, return share links
- `logic_project` — launch, activate, quit, open, save, save_as, new, close, undo, redo, info
- `logic_osc` — control surface: transport, select, mute, solo, volume, pan, bank, channel strip, Channel EQ, plug-in parameters
- `logic_offline` — rename tracks, folders and mixer groups inside the saved project file, Logic closed
- `logic_keys` — escape hatch: any key chord or named Logic command

Resources: `logic://health`, `logic://transport`, `logic://tracks`, `logic://app`,
`logic://markers`, `logic://mixer`, `logic://regions`

## Honest contract
Every write returns `state: confirmed | uncertain | failed`. Only **confirmed** means the change was read back from Logic.
Destructive track ops require an explicit index/name and a confirmed selection, or they refuse.

The MCU surface only ever addresses the eight tracks Logic is currently showing on it — its bank buttons do
nothing here. Which track sits on which strip is **read back from the surface's own display**, never assumed,
and anything it cannot prove is routed through Accessibility instead. An earlier version assumed the bank and
silently moved faders on tracks nobody had asked for.

## Layout
```
logic_mcp/
  server.py          MCPServer, tools, resources
  doctor.py          channel health + fix plan
  target.py          find Logic (desktop / Creator Studio), pid, version
  result.py          confirmed / uncertain / failed envelope
  channels/
    midi.py          rtmidi + CoreMIDI: Logic Virtual In, MCU handshake/LEDs/LCD/faders/banks, MMC
    ax.py            generic Accessibility helpers (walk, find, press, set, dump)
    ax_logic.py      Logic-specific AX map (verified on 12.3 Creator Studio)
    palette.py       track color palette: swatch grid, names/hex, selection-ring readback
    screen.py        pixel readback (screen capture) for what AX can't see
    dialogs.py       macOS save panels, Logic alerts, popups, segmented bar/beat fields
    mouse.py         real HID clicks for controls whose AXPress is inert
    keys.py          CGEvent key posting + Logic default key commands
    applescript.py   osascript lifecycle helpers
  plugins.py         insert plugins, read/write parameters in real units
  audio.py           BS.1770 loudness, true peak, crest, correlation, spectral balance
  leveling.py        stem measurements -> gain and pan moves
  export.py          bounce / stem export, with completion proven on disk
  upload.py          rclone delivery to cloud storage
```
See `FEATURES.md` for the full matrix and `PLAN.md` for the research behind it.

## Gesture and voice control

Hands and speech drive Logic through the same channels, via four small processes. Full write-up in
[`docs/gesture.md`](docs/gesture.md); research and decisions in [`GESTURE_VOICE_PLAN.md`](GESTURE_VOICE_PLAN.md).

**Voice carries the nouns and the verbs; the hand carries the value.** Say *"volume on strings"* and
your hand is that fader until you let go. A DAW has ~1000 commands and ~100 tracks; a hand has six poses
you can hit without looking, so the hand never names anything.

```
handd  (Swift, camera)  --UDP-->  gestured  --unix sock-->  logicpadd  --> Logic
voiced (Python, mic)    --UDP-->     |                          ^
                                     +--UDP--> hud              +-- logic-mcp
```

**The model is never in the gesture loop.** `gestured` writes OSC directly at ~45 ms hand-to-fader; a
model only appears when the voice grammar misses, and then out-of-band on its own thread with a hard
timeout so it cannot stall the hand.

`logicpadd` exists because only one process can hold UDP 9000 plus the Bonjour advert, and Logic
**segfaults** if that advert disappears while it has the device bound. It owns the one surface and
serves both the MCP server and the gesture daemon. With no daemon running everything falls back to
binding directly, exactly as before.

The arbiter is a pure state machine with three rules: no target means no motion; poses are suppressed
while the clutch is closed; and releasing the clutch *freezes* the value rather than resetting it —
losing the hand is treated the same way.

```bash
logicpadd &          # owns the OSC surface; start first
gestured --dry-run   # decides everything, sends nothing
gesture-fakehand     # synthetic hand, no camera needed
gesture-hud          # always-on-top readout
```

## Delivery

`logic_deliver(action="bounce_and_upload", ...)` runs the whole chain and hands back share links:

```
directory + filename + formats=["wav","mp3"]  ->  bounce (one pass, both formats)
                                              ->  verify sizes on disk
                                              ->  rclone copy to remote:folder
                                              ->  verify remote sizes match, return links
```

Measured at 68 s for a 9-bar preview of an 8-track project, WAV + MP3, uploaded and linked.

Uploads go through `rclone` rather than a Drive API tool, because bounced audio is large and binary and
should stream from disk instead of passing through the model's context.

**Uploads go to your own account.** No credentials and no default remote ship with this server. Run
`logic_deliver(action="setup")` for the exact commands; you run them yourself, and the sign-in happens in
your browser under your own Google account — nothing is stored here. Pin a remote with `$LOGIC_MCP_REMOTE`
or pass `remote=`. With several remotes configured and none pinned, delivery refuses rather than guessing
which account your audio should land in.

## Plugin control

Both reference projects declared plugin parameters unreachable. They are reachable, but the mechanics are odd:

* `AXValue` is settable and reports success, yet moves **one internal unit per call** whatever number you pass.
* `AXIncrement` / `AXDecrement` move **ten**.
* Internal units map non-linearly to real ones (frequency is logarithmic), so the engine steps, re-reads the
  plugin's own readout, learns what a step is worth locally, and repeats until it is inside tolerance.

```python
logic_plugins("insert", track=1, plugin="Channel EQ")
logic_plugins("set", parameter="Low Cut Frequency", value="90 Hz")     # -> confirmed, 20.0 Hz -> 90.0 Hz
logic_plugins("set", parameter="Peak 2 Gain", value="-2.5 dB")         # -> confirmed, 0.0 dB -> -2.6 dB
```

Values are checked for unit agreement first — a target in Hz aimed at a control that reads in percent is
refused rather than driven somewhere absurd. Ratio readouts (`4:1`) and signed infinities parse correctly.
Plugin editors are identified by structure (a bypass control plus a close button) rather than by "any window
that is not the arrange window", so an open Mixer is never mistaken for one.

**Coverage varies by plugin, and the tool is honest about it.** Channel EQ names every control, so all 26
parameters and 14 switches are exact. The Compressor leaves its sliders anonymous with the names in separate
text elements and readouts in percent; those are matched by on-screen position and marked `labelled_by:
"position"`. Any name that matches more than one control — Channel EQ has two sliders called `Gain` — is
refused rather than guessed at. A parameter whose EQ band is switched off reports `AXEnabled: false`, and is
either enabled first or refused.

## Auto-leveling

Logic's meters report clipping only, with no numbers, so levels are measured from rendered audio instead —
which is what a human engineer does anyway. Loudness follows ITU-R BS.1770-4 (K-weighting, 400 ms blocks,
absolute and relative gating), alongside 4x-oversampled true peak, crest factor, stereo correlation and a
seven-band spectral balance.

```python
logic_level("plan",  directory="~/stems")            # measure and propose, nothing touched
logic_level("apply", directory="~/stems", dry_run=True)
logic_level("apply", directory="~/stems")            # set the faders, verified in dB
logic_level("export_and_plan", directory="~/stems")  # render stems out of Logic first
```

The reference stem — the lead vocal when it can find one — defines the balance: everything else is placed
relative to it by role, so the mix keeps its character instead of being flattened to a number. Roles are
guessed from stem names. Peaks are checked against a headroom line and moves are pulled back if they would
breach it, and if any stem wants a boost the whole set is shifted down instead so nothing is pushed into
the ceiling.

That shift moves the reference's own fader too — it is fixed *relative* to the others, not pinned at 0 dB.
The plan reports it (`global_shift_db`, and `shifted_by_db` per move), and `gain_uncapped_db` keeps the
pre-shift intent visible, so "this stem wanted to be louder" is not lost. After a downward shift no
`gain_db` is positive; that is the design, not a clamp bug.

Measured on a real 5-stem session (460 s each): 6 s to analyse, 14 s to apply.

```
reference: VOX (vocal) at -17.3 LUFS
  Other    other     -16.7 LUFS   gain  -6.6 dB   pan +25
  GTR      guitar    -18.3 LUFS   gain  -5.0 dB   pan -40
  Bass     bass      -15.1 LUFS   gain  -4.7 dB
  Drums    drums     -15.3 LUFS   gain  -4.0 dB
  VOX      vocal     -17.3 LUFS   gain  +0.0 dB
```

Gains land within about 0.2 dB and are confirmed against Logic's own fader readout. Pan goes over OSC when
the track is on the surface bank, where the readback is Logic's own value and the result is `confirmed`;
otherwise it falls back to the track-header slider, which only moves by Increment/Decrement and settles
within about 4 of 127, and that path stays `uncertain` rather than overstating itself.

## Mix engine

Plain language in, stock plug-in settings out. *"cut the mud in the strings"* resolves the STRINGS family
track, opens or creates its Channel EQ, and drives a recipe of parameter targets with readback.

34 recipes, each carrying synonyms, the plug-in it needs, per-source frequency overrides
(vocals/strings/drums/bass/guitar/keys/mix), a caution, and cited sources. Third-party plug-ins work —
FabFilter Pro-Q 3 lands on 300.00 Hz / −3.00 dB / Q 1.000 exactly. Targets are reached by bisection against
the display string Logic echoes back, so the final number is Logic's own quantisation rather than ours.

Direction is never assumed: the DeEsser's Range runs backwards (0.0 = 25 dB), so the engine probes both
endpoints, requires a fresh echo from each, works out which way the parameter runs, and refuses targets
outside the achievable range.

## Dialogue cues → markers

Finds where speech starts and stops in a mixed programme and writes the result into Logic as markers.

```
audio ──► detect_cues()  ──►  ASR (optional, pluggable)  ──►  cue-marker WAV  ──►  Logic markers
          no model, language-free
```

Detection never looks at phonemes or words, so the same code works in any language: it combines formant-band
energy concentration (300 Hz–3.4 kHz) with syllabic amplitude modulation (3–8 Hz), both as percentile ranks
within the file rather than fixed dB, so it self-calibrates to any programme level. Put 132 Tamil dialogue
markers into a real episode.

Cues are capped at `max_duration` and split at their least speech-like interior frame, so a marker never
covers half a scene. A measured music gate (`off` / `safe` / `strict`) drops cues whose spectrum is too
steady to be speech — `safe` removes about 42 % of music false positives at a cost of about 5 % of dialogue,
and rejected segments stay in the payload with their reason so a wrong call is auditable.

> The gate is deliberately biased toward recall: a spurious marker is one click to delete, a missing one is
> a missed line. An earlier modulation-based gate that "obviously" made sense cut a real 132-cue run to 11.

## Tests

```bash
uv sync --group dev
uv run pytest              # runs with Logic CLOSED and no Accessibility grant
uv run pytest -m live      # the few that need the app
```

The suite covers the parts that decide what is true: the result envelope, the OSC wire format and its
measured address quirks, the ProjectData binary layout (against a synthetic file built independently to the
documented format), dialogue detection, cue WAVs including UTF-8 Tamil labels, loudness maths, recipe
resolution and which cloud account uploads go to.
