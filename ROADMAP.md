# logic-mcp feature map & plan

Probed live on Logic Pro 12.3 Creator Studio, 2026-09-03. Status: ✅ shipped · 🔬 probed & proven reachable · 🟡 partial · ⬜ planned · ⛔ no known path

## Three findings that change the design

**1. Plugin parameters are fully addressable.** Both reference projects concluded this was impossible and shipped refusals. A plugin window exposes every control as a named element with a real-unit readout:

```
AXSlider  desc='Peak 2 Frequency'  value=375.0  AXValueDescription='250 Hz'   min=0    max=1050
AXSlider  desc='Peak 2 Gain'       value=240.0  AXValueDescription='0.0 dB'   min=0    max=480
AXSlider  desc='Peak 2 Q'          value=20.0   AXValueDescription='0.30'     min=0    max=127
AXCheckBox desc='Low Cut'          value=False
AXPopUpButton value='Default Preset'
```

Write mechanics: `AXValue` is settable but moves **one internal unit per call** regardless of the value passed. `AXIncrement`/`AXDecrement` move **ten**. So coarse with Increment, fine with AXValue, verify against `AXValueDescription`. ~10 calls to hit any target exactly.

**2. MIDI recording into a real region works.** Proven end to end: create instrument track → arm via MCU strip → MCU record → send chords over the virtual port → stop. A region appears and Logic's own undo history reads `Undo MIDI Recording`. This is the composing unlock — generated MIDI becomes real, editable, bounceable material instead of a one-off audition.

**3. Regions are readable.** Each track is an `AXLayoutArea` titled `Track N “Name”` whose `AXLayoutItem` children are the regions, carrying name, state, and screen rect. Rect x/width converts to bar positions once calibrated against the ruler. That gives arrangement awareness: what is where, how long, which tracks are empty.

---

## A. Audio engineering — leveling & panning

The mixer channel strip is much richer than the track header and is the correct surface for this work:

| Control | Range | Readout |
|---|---|---|
| `volume fader` | 0–233 | `AXValueDescription` in dB |
| **`pan`** | **-64 to +63** | value description, L/C/R |
| `input gain` | -12 to +12 | dB |
| `peak level meter` | — | clipping status only, **no number** |
| `gain reduction meter` | — | presence only |

**The level problem and its answer.** Logic exposes no numeric meter, so live metering is out. The right path is the one a human engineer would use anyway — measure the audio, not the meter:

1. `File > Export > All Tracks as Audio Files…` writes one stem per track (destination is a standard macOS panel, drivable).
2. Analyse offline: integrated LUFS, short-term LUFS, true peak, RMS, crest factor, spectral centroid, stereo correlation per stem.
3. Compute moves: target each stem to a role-appropriate loudness (lead vocal loudest, pads back), pan by frequency overlap and stereo width so competing sources separate.
4. Apply via the mixer fader (dB readback) and pan slider (-64..+63), each verified.
5. Re-bounce and confirm the mix bus lands on target.

That is a genuine auto-gain-staging and auto-panning engine, and every piece is already reachable. ⬜ to build.

## B. Composing helper

| Feature | Path | Status |
|---|---|---|
| Send notes, chords, timed sequences, CC, bend, program, SysEx | virtual MIDI port | ✅ |
| **Record generated MIDI into a region** | arm + MCU record + send + stop | 🔬 proven |
| Chord generation (progressions, voicings, inversions, borrowed chords) | our own theory engine → MIDI | ⬜ |
| Melody / counter-melody / bassline / arpeggio generation | same | ⬜ |
| Humanisation (timing, velocity curves, strum) | timing of sent events | ⬜ |
| **Chord Track** | `Track > Global Tracks > Show Chord Track` | 🔬 probe next |
| Session Players (Drummer, Bass, Keyboard) | `Track > New Session Player SI Track…` | 🔬 |
| Quantize, groove | Edit / Functions menus | ⬜ |
| Read back what was recorded (note data) | not in the AX tree | ⛔ — bounce or export MIDI to inspect |

Composing loop worth building: propose a progression in the project's key and tempo → record it to a new track → bounce that track → analyse → refine. The agent hears its own output.

## C. Movie / film scoring

| Feature | Path | Status |
|---|---|---|
| Open / remove movie | `File > Movie > Open Movie…` | 🔬 |
| Movie track (see picture against the timeline) | `Global Tracks > Show Movie Track` | 🔬 |
| **Scene cut markers** (auto-detect cuts as hit points) | `Navigate > Other > Create Movie Scene Cut Markers` | 🔬 high value |
| Import audio from movie / export audio to movie | `File > Movie` | 🔬 |
| Tempo track + beat mapping (hit a cut on a downbeat) | `Global Tracks > Tempo / Beat Mapping` | ⬜ |
| Timecode position readout | control bar has an SMPTE display alongside bars/beats | 🔬 |

Scoring flow: open the movie, generate scene cut markers, read them as hit points, then build tempo and markers so musical accents land on the cuts. Logic does the cut detection, so the hard part is free.

## D. Track organization

| Feature | Path | Status |
|---|---|---|
| Create audio / instrument / external MIDI track | Track menu | ✅ |
| Rename, delete (fail-closed, handles the "delete regions?" alert) | Track menu + fresh-title guard | ✅ |
| Color, 96-swatch palette by name/hex/index | Assign Track Color… + HID click + ring readback | ✅ |
| Select, mute, solo, arm, volume in dB, pan | MCU strip + AX readback | ✅ |
| **Track stacks** (folder & summing) | `Track > Create Track Stack…`, `Flatten Stack` | 🔬 |
| **Groups** | `Mix > Create Group`, `Group Settings` | 🔬 |
| Track icons | `Track > Assign Track Icon…` | 🔬 |
| Hide / unhide, sort, delete unused | Track menu | 🔬 |
| Bulk color readback across the track list | header icon tint sampling | 🟡 ~25% hit rate |

## E. Markers & arrangement

| Feature | Path | Status |
|---|---|---|
| Create / rename / delete marker, marker list | `Navigate` menu | 🔬 |
| **Arrangement track** (Intro/Verse/Chorus sections) | `Global Tracks > Show Arrangement Track` | 🔬 |
| Signature track, tempo track | Global Tracks | 🔬 |
| Playhead, cycle range, locators | AX control bar | ✅ / 🟡 |

## F. Mixing & plugins

| Feature | Path | Status |
|---|---|---|
| **Insert plugin by name** | `Mix > Search and Add Plug-in…` — search field + result rows, fully readable | 🔬 proven |
| **Read/write any plugin parameter in real units** | named sliders + AXValueDescription | 🔬 proven |
| Plugin presets, bypass, compare, copy/paste settings | plugin window controls | 🔬 |
| Sends, input, output, group assignment | mixer strip buttons | 🔬 |
| Mastering Assistant | `Mix > Mastering Assistant…` | ⬜ |
| Automation create / convert / delete | `Mix` menu | ⬜ probe needed |

Plugins on this machine: Channel EQ, Compressor, ChromaVerb, Tape Delay, Chorus, Overdrive, Pro-L 2, Raum, ShaperBox 3, CLA-2A, CLA Effects, OneKnob Pumper, Xvox Pro.

## G. Export & delivery

`File > Bounce > Project or Section…` — readable dialog with **Uncompressed, MP3, M4A checkboxes that can be ticked together**, so WAV and MP3 come from one pass. Plus File Type, Bit Depth, Sample Rate, Add to Project. Cancel/OK.

Google Drive is connected and verified against the real account.

**Target chain — "open the project, export mp3 and wav, upload both to Drive":** open → bounce dialog (tick both formats) → save panel (`Cmd+Shift+G`, type path) → wait for completion → verify files on disk → upload → return links. Every step reachable; the chain itself is ⬜ and is the highest-value build.

## H. Genre templates

1. **Logic's own** — `File > New from Template…`, `File > Save as Template…`. 🔬
2. **Programmable genre recipes** — a JSON recipe our server executes. More useful than a static template because it adapts. ⬜

```json
{ "genre": "tamil-melody", "tempo": 78, "key": "C Minor", "time": "4/4",
  "markers": [{"bar":1,"name":"Intro"},{"bar":9,"name":"Pallavi"},{"bar":25,"name":"Charanam"}],
  "stacks": [{"name":"DRUMS","type":"summing","tracks":["Kick","Snare","Hats","Perc"],"color":"dark red"}],
  "tracks": [{"name":"Lead Vox","type":"audio","color":"blue","fx":[
      {"plugin":"Channel EQ","params":{"Low Cut Frequency":"80 Hz","Peak 3 Gain":"+2.5 dB"}},
      {"plugin":"ChromaVerb","params":{"Mix":"18 %"}}]}] }
```

## Hard-won mechanics

- **AXPress is inert on many Logic controls** (color swatches, some slot buttons). A real HID mouse click works. Pid-posted clicks often do not.
- **Menu titles only re-validate when the menu opens.** Open via AXPress, read, then press or cancel. Fresh `Undo <action>` is also a free forensic readout of what just happened.
- **Plugin slot popups are custom-drawn layers with no AX tree.** Use the search dialog instead.
- **Confirmation alerts** are readable: `AXWindow desc='alert'`, buttons `action-button-1` (confirm) / `action-button-2` (cancel). Destructive flows must read the alert text before answering.
- **Each process gets its own MIDI bus**, so MCU state is per-process. Always read transport state through the server, never a side script.
- **Locked screen / sleeping display** removes all windows from Accessibility and blanks captures. Doctor checks this first.
- `CGGetDisplaysWithRect` must not be the first CoreGraphics call in a process.

## Not reachable

| Thing | Why |
|---|---|
| Note-level MIDI data inside a region | not in the Accessibility tree — export MIDI and parse instead |
| Numeric level metering | meters report clipping only — measure bounced audio instead |
| Plugin slot popup menus | custom-drawn layer |

## Build order

1. **Export chain** — bounce, save panel, completion detection, Drive upload. Named use case, every piece proven.
2. **Plugin engine** — insert by name, read/write params in real units, presets, bypass.
3. **Audio-engineer loop** — export stems, analyse, compute gain and pan, apply, re-verify. Depends on 1 and 2.
4. **Composing engine** — chord/melody theory → record to region. Foundation proven.
5. **Structure** — stacks, groups, icons, markers, arrangement track.
6. **Genre recipes** — the JSON layer composing 2 through 5.
7. **Scoring** — movie import, scene cut markers, tempo mapping to hit points.
