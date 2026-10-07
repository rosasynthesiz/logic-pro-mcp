# UI layout fingerprints

Captured 2026-09-06 from Rosa's Logic Pro Creator Studio 12.3 so the code knows what a "normal" screen looks
like and can tell when a project or user has customised it.

- `control_bar_custom.json` / `control_bar_default.json` — every control-bar element with its rect. Rosa's
  customised bar turned out to be identical to the saved default (Customize Control Bar > Apply Defaults changed
  nothing), so both files list the same 34 items.
- `control_bar_customize_*.json` — the Customize Control Bar and Display popover's checkbox states.
- `track_header_configure_custom.json` — Configure Track Header popover: ON = Volume, Solo, Mute, Record Enable,
  Input Monitoring, Track Icons, Track Numbers, Control Surface Bars. OFF = Groove Track, Color Bars, On/Off,
  Protect, Freeze, Track Alternatives. That popover has only Revert, no factory-reset button.
- `track_header_default.json` — the AX children of a track header under that configuration.

Rule for code: never assume a layout. Read the live AX tree, then compare against these files to explain a
mismatch (e.g. the older project snapshot had On/Off and Hide buttons in the header, which put the record
button under the point the selector clicks).

Both popovers live under the application element as `AXPopover`, not under the window.
