---
name: logic-pro-mcp
description: Use when the user wants to control Logic Pro on macOS through the logic-pro MCP server: transport, tracks, mixer levels, plug-in parameters, bounce or stems, delivering a bounce to cloud storage, measuring or balancing levels, or putting dialogue markers into a project. Also use when a Logic tool call fails or returns an uncertain result.
---

# Logic Pro MCP

Guidance for the `logic-pro` MCP server (12 tools, 7 resources). It talks to Logic Pro the way hardware does (OSC, Mackie Control over CoreMIDI, Accessibility, key commands, AppleScript) and reads results back from Logic.

## Start and troubleshoot

- If anything fails or looks off, call `logic_system` (health / doctor, permissions, midi_ports) before retrying. It reports which channels are ready and what the user must click.
- `logic_system` runs the same check as the standalone `logic-pro-mcp-doctor` command from the project's GitHub README.
- The server needs a Mac with Logic Pro, and the app running the server needs **Accessibility** and **Automation > System Events** permission (System Settings > Privacy & Security). If the doctor says a permission is missing, tell the user to grant it. Do not work around it.
- Do not retry a failed call blindly. Read the result, say what failed, and fix the cause.

## The honest contract

Every write returns `state: confirmed | uncertain | failed`.

- **confirmed**: the change was read back from Logic. Only this state means it happened. Report it as done.
- **uncertain**: the change may or may not have happened. Say so. Check with a read (`logic_tracks` list, `logic://tracks`, `logic://mixer`) before building on it.
- **failed**: it did not happen. Report it and do not continue as if it did.

Never describe an uncertain result as success.

## Tracks

- Destructive track operations (such as delete) need an explicit track index or name and a confirmed selection, or the server refuses. Always list tracks first, name the exact track, and confirm with the user before deleting.
- Track volume is set in dB with `logic_tracks` `set_volume`. Colors use `set_color` with a palette index, a name like `dark green`, or a hex value.

## Mackie Control (MCU) limits

MCU only addresses the **eight tracks Logic currently shows on the surface**. Its bank buttons do nothing here. The server reads which track sits on which strip from the surface's own display, and routes anything it cannot prove through Accessibility. Do not assume a track number maps to a strip. For a track outside the visible eight, expect the Accessibility path (or OSC) and check the `state`.

## Plug-ins

- `logic_plugins` inserts by name and describes, gets and sets parameters in **real units** (dB, Hz, %, ratios like `4:1`).
- Give the value with its unit, for example `"90 Hz"` or `"-2.5 dB"`. A target in Hz aimed at a control that reads in percent is refused. If that happens, describe the plug-in's parameters and fix the unit, do not force it.
- Coverage varies by plug-in. Channel EQ names every control. The Compressor's sliders are matched by on-screen position (`labelled_by: "position"`). A name that matches more than one control (Channel EQ has two sliders called `Gain`) is refused rather than guessed, so use the more specific name.
- A parameter on a band that is switched off may be refused or enabled first. Read the result.
- The final number is Logic's own quantisation: compare the confirmed readout, not the request.

## Levels

Logic's meters give no numbers, so levels are measured from rendered audio with `logic_level` (LUFS, true peak, crest, correlation, spectrum).

1. `plan` first. It measures and proposes, and touches nothing.
2. `apply` with `dry_run=True` to preview.
3. `apply` to set the faders. Gains are confirmed against Logic's fader readout.

`export_and_plan` renders stems out of Logic first. The reference stem (the lead vocal when found) sets the balance. If any stem needs a boost, the whole set is shifted down instead, so after a downward shift no `gain_db` is positive. That is by design.

## Delivery

`logic_deliver` with `action="bounce_and_upload"` bounces, verifies sizes on disk, uploads with `rclone`, verifies the remote sizes and returns share links.

- Uploads go only to the user's **own rclone remote**, and only when they ask. No credentials or default remote ship with the server.
- If setup is missing, run `logic_deliver(action="setup")` and give the user the commands. The user runs them and signs in under their own account.
- Pin a remote with `$LOGIC_MCP_REMOTE` or `remote=`. With several remotes configured and none pinned, delivery refuses rather than guessing. Ask the user which remote to use. Do not pick one.

## Dialogue markers

Dialogue cue detection finds where speech starts and stops and writes the result into Logic as markers. It is language-free and tuned to catch every line: a spurious marker is one click to delete, a missing one is a missed line. The music gate (`off` / `safe` / `strict`) drops music false positives, and rejected segments stay in the payload with their reason, so check those before saying a line was missed.

## Other notes

- `logic_offline` renames tracks, folders and mixer groups inside the saved project file with Logic closed. Use it only when Logic is closed.
- `logic_keys` is an escape hatch for any key chord or named Logic command. Prefer a dedicated tool when one exists.
