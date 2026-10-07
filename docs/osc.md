# OSC control surface (TouchOSC "LogicPad" emulation)

Logic ships an Apple-made control-surface plug-in, `TouchOSC.bundle`, with a FIXED OSC address map
(`logic_mcp/osc_map.txt`, 651 paths read out of the binary). No Learn step, no MIDI: UDP in both directions,
with Logic streaming its state back (track names, select/mute/solo, faders, bar, SMPTE time, transport).
`logic_mcp/channels/osc.py` is our device; `logic_osc` in `server.py` is the MCP tool.

## How Logic binds a device (measured on Creator Studio 12.3)

1. Logic browses Bonjour `_osc._udp` and pattern-matches the SERVICE NAME the way TouchOSC Mk1 announced
   itself: `<name> [iPad] (TouchOSC)` -> model "TouchOSC LogicPad", `[iPhone]` -> "TouchOSC LogicTouch".
   The strings "TouchOSC LogicPad" inside the plug-in are display names; advertising them is found and
   resolved (`Did resolve Address ... port 9000`) but never bound.
2. Logic resolves host:port and shows them as Host Name / Output Port in Control Surfaces > Setup.
3. The first OSC *press* (value 1.0; a 0.0 release is ignored) that reaches Logic's receiver UDP 7000
   from that resolved host:port raises the alert
   `OSC app "TouchOSC" on "<name>" wants to connect to Logic Pro.`  Connect / Don't connect.
   `ax_logic.press_osc_connect` clicks Connect. Packets from any other source are dropped silently.
   Loopback is refused: advertise and send from the LAN IPv4 (`dns-sd -P <name> _osc._udp local 9000
   logicpad.local <lan ip>`; a proxy record, so the host name is not this Mac's own).
4. After Connect the device is persisted in `~/Library/Preferences/com.apple.logic.pro.cs`, Logic logs
   `Starting OSC ports for device: In=7000, out=9000`, pushes ~340 state messages, keeps a UDP socket
   connected to our port (that socket is our passive "bound" check via `lsof`) and streams feedback.
   Next launches re-bind without the alert as long as the same service name/host is advertised.

Hidden trace: `defaults write com.apple.mobilelogic TraceOSC -int 255`, then
`/usr/bin/log stream --process "Logic Pro Creator Studio" --level debug | grep "(Logic)"`
(zsh's builtin `log` shadows the real one). Message contents are `<private>`; events are not.

## Do not
* Kill or restart the Bonjour advert while Logic has the device bound: Logic segfaults on the main thread
  (crash 2026-09-06 04:31). `advertise()` adopts a running advert, `close()` leaves it up, `forget()`
  exists for when Logic is not running.
* Use `/1/stop` as a probe: Stop while stopped moves the playhead to the project start.
* Expect `/1/select/1/N` to echo when that strip is already selected (no change, no echo).

## Behaviour notes
* The 8 strips are the current fader bank; `bank+`/`bank-` move by 8, `track+`/`track-` by 1. Logic
  only pushes bank state on device creation, so a re-bound device is refreshed with bank+ then bank-.
* Faders: `/1/volumeN` 0..1, echoed on change. 0.75 ~ +0.9 dB, 0.72 ~ 0 dB, 0.6 ~ -2.9 dB (Logic's
  fader law; read the exact dB back from the track header via `ax_logic.track_db`).
* Mute/solo/select are button presses that toggle (mute, solo) or select; the echo carries the new state.
* `/1/record` starts recording on armed tracks -- only on explicit request.
## Pages (what each one actually carries, measured)

A bare `/N` message switches the surface's page; Logic answers with that page's state.

| page | contents | wrapped as |
|---|---|---|
| `/1` | mixer bank of 8: transport, volume/level, select/mute/solo, bank+/-, bar, time | `status`, `transport`, `bank`, `select`, `mute`, `solo`, `volume` |
| `/2` | same bank plus per-strip pan and 3 sends each | not wrapped (raw `send`) |
| `/3` | SELECTED track's channel strip: 12 insert slots, the focused plug-in and its 16 parameters, sends, pan, mute/solo/arm | `strip`, `param`, `param_set`, `plugin` |
| `/4` | selected track's Channel EQ: 8 bands with gain/freq/Q (+slope on the filters) | `eq`, `eq_set` |
| `/5` | a second plug-in parameter page (24 slots) | not wrapped -- `/3` covers it |

Measured quirks in these pages:
* **Logic de-duplicates feedback per address.** It only sends a value that changed since it last sent it,
  so a freshly started process sees an empty page even after switching to it. `full_refresh()` hops the
  selection one track away and back, which changes every value and forces a full resend; `_osc_ready`
  calls it once per connection.
* **Band gain is off by one:** `/4/gain/3` drives band 4, i.e. `/4/gain/<band-1>`, and 0.5 = 0 dB. Bands 1
  and 8 (the filters) have no gain at all -- only freq, Q and slope.
* **Insert slots are reversed:** insert slot 1 echoes as `/3/insertbypass/12/1`, i.e. column `13-slot`.
  A bypassed plug-in's name comes back prefixed with `*` ("*Channel EQ"), which is a second confirmation.
* `/4/lowq` and `/4/loqval` break the `<band>q` / `<band>qval` naming of every other band.
* Setting a parameter to a real-world number (300 Hz, -3 dB, Q 1.0) is a closed loop: `set_target` bisects
  the normalised 0..1 value against the display string Logic echoes back. 14 steps land exactly on
  Logic's own quantisation, so the readback is Logic's value, not our guess.

## Marker import from a cue WAV — rules

`Navigate > Other > Import Marker from Audio File` reads the RIFF `cue `/`labl` chunks of the SELECTED audio
region and creates project markers from them. UTF-8 labels survive intact (verified with Tamil). It is the
fast path: hundreds of named markers in one action instead of hundreds of UI steps.

Two behaviours that are not obvious and both bite hard:

* **It REPLACES the whole marker set, it does not merge.** A project with 29 markers importing 132 cues ends
  with 132, not 161. To keep existing markers, read them first and write them into the same cue file.
* **The import lands on the SELECTED track at the playhead.** NEVER import onto a track that already holds
  audio: make a new empty audio track, import onto that, and delete the whole track afterwards. Measured on
  2026-09-08 -- the carrier WAV landed exactly on top of the dialogue region on track 1 (same start, width
  within 3 px), so every click in the arrange selected the dialogue region instead, and removing the temp
  region by clicking risked deleting production audio.

To remove a temp carrier file safely, target it by NAME: `Window > Open Project Audio`, select its row, then
`Audio File > Delete File`. Logic confirms with exact counts ("1 audio file", "1 used region to remove"),
which is the check that proves nothing else is going with it.
