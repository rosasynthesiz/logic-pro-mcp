"""Control channels. Each channel is a thin, dependency-light wrapper around one macOS mechanism.

midi        CoreMIDI via python-rtmidi: notes/CC/PB/PC/SysEx into Logic, MMC, MCU emulation + readback
applescript osascript: app lifecycle (activate/quit/open), System Events queries
keys        CGEvent key posting to Logic's pid (key commands)
ax          Accessibility API: read UI state, press buttons, set values, menu navigation
"""
