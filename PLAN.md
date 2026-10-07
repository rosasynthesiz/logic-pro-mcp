# AI control of Logic Pro — research + plan (2026-09-03)

> **Update, same day:** decision changed from "fork MongLong" to **own build** in Python (`logic_mcp/`).
> The research below still holds; the reference repos were used read-only. Current status lives in `README.md` and `FEATURES.md`.
> Biggest discovery since: Logic auto-installs a Mackie Control surface if you answer its MIDI device query, so the
> "manual MCU setup" step in Phase 0 is gone.

## TL;DR
- No official Apple API. Every working project drives Logic through macOS side-channels: virtual MIDI (Mackie Control emulation), Accessibility, CGEvent key commands, AppleScript (app lifecycle only, no dictionary), Scripter (JS MIDI FX), OSC (control-surface only).
- Don't build from scratch. Fork **MongLong0214/logic-pro-mcp** (Swift, 99k lines, 3.9k tests, v3.15.0 on 2026-08-31, 7 channels, verified readback, brew formula, in Anthropic's Claude for Open Source program).
- Blocker: your install is **Logic Pro Creator Studio 12.3** (`com.apple.mobilelogic`). That server hard-refuses it (`shipVariants = [.desktop]`, `preferredLogicApp` only matches `com.apple.logic10`). Fix is a small patch; the Creator Studio Accessibility code paths already exist in the repo.
- Hermes = harness (memory, skills, cron, orchestration). Claude Code = builder/debugger. Both talk to the same MCP server over stdio.

## Local state (verified)
| Item | Value |
|---|---|
| Logic | Creator Studio 12.3, bundle `com.apple.mobilelogic`, process "Logic Pro Creator Studio", macOS 26.6.2 |
| Desktop Logic (`com.apple.logic10`) | not installed |
| AppleScript dictionary (sdef) | none. Only generic activate/quit/open |
| Hermes | v0.21.0 at `~/.hermes/hermes-agent`, config `~/.hermes/config.yaml`, no `mcp_servers` yet |
| Claude Code MCPs | no logic/hermes registered |
| Toolchain | swift, uv, python 3.14, node 26, brew (no gh) |

## Candidates compared
| Repo | Size | Channels | Creator Studio | Verdict |
|---|---|---|---|---|
| MongLong0214/logic-pro-mcp | 99k Swift, 1.6k commits | MCU, AX, AppleScript, CoreMIDI, CGEvent, Scripter, Key Commands | recognised, refused by design (ADR-001) | **fork + patch** |
| koltyj/logic-pro-mcp | 5.8k Swift, 13 commits | CoreMIDI, AX, CGEvent, AppleScript, OSC | accepts bundle ID, but process name hardcoded "Logic Pro" | fallback if fork fights back |
| aircrack-ng-debug/Logic-Pro-MCP | TS + Swift helper | AppleScript, AX, virtual MIDI | untested | dead (3 commits) |

Reference designs worth stealing from: ahujasid/ableton-mcp (socket + remote script, ~2.5k stars), TwelveTake-Studios/reaper-mcp (176 tools, workflow helpers), ptaczek/daw-mcp (Bitwig/Ableton MIDI clip editing).

## Architecture
```
You / Hermes chat
   └─ Hermes agent (harness: memory, skills, cron, user model)
        ├─ MCP client ──stdio──► LogicProMCP (fork) ──► Logic Pro Creator Studio
        │                          7 channels, State A/B/C readback, doctor
        └─ delegate_task / `claude -p` ──► Claude Code (patch server, write skills, debug)
                                              └─ same MCP server registered via `claude mcp add`
```
Harness rules (Anthropic long-running-agent pattern, applied here):
- Only State A (confirmed readback) counts as done. B = retry or ask. C = stop.
- Read resources (transport/tracks/mixer) before every mutation. Never assume state.
- Skills hold workflow recipes. Memory holds project conventions (tempo, track naming, bus layout).
- Doctor runs at session start. Degraded channel = degraded plan, not silent failure.

## Phases
### 0. Prereqs (30 min, manual)
- System Settings → Privacy: grant Accessibility, Automation (Logic + System Events), Input Monitoring to the built binary and to Terminal/Claude.
- In Logic: register the `LogicProMCP-MCU-Internal` control surface; add the bundled Scripter insert (only needed for plugin-parameter writes).

### 1. Fork + patch + build (Claude Code, 1–2 h)
- Fork MongLong0214/logic-pro-mcp.
- Patch `Sources/LogicProMCP/Utilities/SetupDoctor+LogicSupport.swift` (`preferredLogicApp` → also accept `.creatorStudio`) and `Qualification/ReleaseQualificationAttestation.swift` (`shipVariants`), ideally behind env `LOGIC_PRO_MCP_ALLOW_CREATOR_STUDIO=1` so upstream merges stay clean.
- `swift build -c release`, then `LogicProMCP doctor --strict`. Target: 5 channels green, 7 with Scripter + Key Commands.
- Run the test suite; note which AX paths break on Creator Studio (project chooser is the known one, issue #571).

### 2. Register (10 min)
```bash
claude mcp add --scope user logic-pro -- /path/to/LogicProMCP
```
`~/.hermes/config.yaml`:
```yaml
mcp_servers:
  logic-pro:
    command: "/path/to/LogicProMCP"
    args: []
```
Then `hermes mcp test logic-pro`.

### 3. Smoke (30 min)
Play/stop → tempo → new software-instrument track → write 4-bar MIDI → read back tracks resource → bounce. Each step must return State A.

### 4. Harness layer (Hermes, ongoing)
- Skill `logic-pro-producer`: recipes (new session from template, sketch chords → MIDI, mix-bus setup, batch export), plus the State A/B/C rule.
- Memory: your defaults (BPM, key, track colors, bus routing).
- Cron: nightly export audit via `logic_project` export tools.
- Optional: `hermes mcp serve` so Claude Code can call Hermes memory/skills back.

### 5. Known gaps (route to human)
Automation curves, arbitrary plugin params beyond Scripter, sends (`set_send` not exposed), marker rename. Don't fake these.

## Sources
- https://github.com/MongLong0214/logic-pro-mcp (README, docs/SETUP.md, docs/adr, docs/roadmap)
- https://github.com/koltyj/logic-pro-mcp
- https://github.com/aircrack-ng-debug/Logic-Pro-MCP
- https://github.com/ahujasid/ableton-mcp · https://github.com/TwelveTake-Studios/reaper-mcp · https://github.com/ptaczek/daw-mcp
- https://github.com/NousResearch/hermes-agent (docs/user-guide/features/mcp.md, skills/autonomous-ai-agents/claude-code)
- https://github.com/DHKun/hermes-mcp
- https://support.apple.com/guide/logicpro/osc-message-paths-ctlsf67f4bdc/mac · https://support.apple.com/guide/logicpro/scripter-api-overview-lgce3905a48c/mac
- https://www.apple.com/newsroom/2026/06/apple-creator-studio-gets-smarter-faster-and-more-connected/
- https://github.com/anthropics/cwc-long-running-agents · https://github.com/ai-boost/awesome-harness-engineering
