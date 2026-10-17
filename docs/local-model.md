# Local model profile

Run Logic Pro MCP from a small model on this Mac, with no cloud model in the loop: Qwen3.5 4B through a
local Ollama first, 9B as a comparison. This document is the design and the configuration; it is honest
about what has and has not been exercised.

**Status (2026-10-17):** the profile, its policy and its fixture tests exist and pass. No model has been
run against it, and nothing here has been driven against a running Logic. "Works with Qwen" is a claim the
acceptance gate at the end has to earn.

## Start it

```bash
uv run logic-pro-mcp --profile local      # or: uv run logic-pro-mcp-local
LOGIC_MCP_PROFILE=local uv run logic-pro-mcp
```

`--profile full` is the default so every existing `claude mcp add` entry keeps the surface it was
registered with. The full profile is the trusted-operator surface: every dispatcher, including raw key
chords, raw MIDI, menu presses, recording, deletion, cloud upload and offline binary edits. It is not a
safe surface for an unattended small model, and it is not made one by prompt instructions.

## The eight tools

| Tool | Arguments | Confirmed by |
|---|---|---|
| `health` | – | passive: Logic running, project title, which channels work; never publishes a MIDI port |
| `read_transport` | – | passive: playing / recording / tempo / bar; `null` = not known |
| `list_tracks` | `offset`, `limit` (≤ 50) | index, name (≤ 64 chars), muted, soloed, armed, volume_db (−100 = fader at −∞) |
| `play` | – | Logic reads back playing = true |
| `stop` | – | Logic reads back playing = false |
| `set_tempo` | `bpm` 5..990 | the tempo slider read back |
| `set_mute` | `track_index`, `track_name`, `muted` | the track's Mute control read back after a fresh re-resolve |
| `set_gain` | `track_index`, `track_name`, `gain_db` −60..+6, ≤ 12 dB per call | Logic's own dB readout within 0.3 dB |

Every write returns `state: confirmed | uncertain | failed`. `uncertain` with `accepted: true` means the
request left this process and nothing proved its effect; the right move is `read_transport` or
`list_tracks`, never a second write. `failed` with `refused: true` means the server declined before
anything was sent, and `fix` says why.

Not in the profile, on purpose: raw keys, raw MIDI/MCU/OSC sends, menu presses, record, arm, solo,
track create/delete/rename/colour, project open/save/quit, bounce and upload, plug-in edits, offline
`.logicx` edits. There is no generic command tool, and no `approved`/`confirm` argument: a model cannot
authorise itself, and an unknown key is a refusal, not something ignored.

## What the server enforces (not the prompt)

* **A separate server.** `logic_mcp/local.py` builds its own MCP server whose `tools/call` handler knows
  eight names. `logic_keys`, `logic_tracks` or any other name is refused at the handler with
  `refused: true`; the full dispatchers are never registered. Hiding tools from `tools/list` was not the
  mechanism, because a client can call whatever it likes.
* **Validation first.** Hand-written JSON schemas, `additionalProperties: false`, and a validator that
  rejects unknown keys, missing required keys, booleans where numbers go, NaN/inf, out-of-range numbers,
  blank or over-long names. Nothing reaches a channel until the arguments are clean.
* **Preflight on every write.** Screen unlocked, Logic running, Accessibility trusted, a project window
  up, no alert window and no sheet on the project window. Any of those fails closed.
* **Exact targets.** `set_mute` and `set_gain` take index AND name and both must describe the same row of
  a fresh read. Two tracks called "Vocal" are refused by name; a contradiction between index and name is
  refused; substring matching is never used for a write. The readback re-resolves by index and name, and
  the project identity (document path + title) is recorded before the write and checked after it.
* **One mutation at a time.** Writes run under a guard: an in-process re-entrant lock plus an advisory
  `flock` shared by every logic-pro-mcp process of this user. A second caller is refused after a deadline
  instead of queueing. The guard cannot stop a human at the keyboard, the gesture daemon or software that
  does not take it; the fresh readback is what catches those. It is not ownership of Logic.
* **Passive reads.** `health` uses `doctor.run(probe_mcu=False)`, `read_transport` uses
  `transport_state(passive=True)`; neither publishes the MCU ports (which makes Logic install a control
  surface) or binds the OSC surface. The `logic://transport` resource is passive now too.
* **Bounded output.** Paged track lists, names cut at 64 characters, readback trimmed to a few keys, free
  text cut at 300 characters, and no colours, regions or audio.

## Size of the surface

Measured from the servers' own `tools/list` output (characters, not tokens; descriptions and schemas only,
no envelope):

| Profile | Tools | Enum choices | Schema chars | Description chars |
|---|---|---|---|---|
| full | 12 | 107 | 9,764 | 7,926 |
| local | 8 | 0 | 1,050 | 911 |

Re-measure after any change:

```bash
uv run python -c "import asyncio; from logic_mcp import server, local; \
ts = asyncio.run(server.mcp.list_tools()); \
print('full', local.schema_stats([{'name': t.name, 'description': t.description, 'inputSchema': t.input_schema} for t in ts])); \
print('local', local.schema_stats())"
```

## Ollama on this Mac

Ollama binds `127.0.0.1:11434` by default; leave `OLLAMA_HOST` unset so inference never listens on the
LAN. Cloud features are off with `OLLAMA_NO_CLOUD=1` (or `"disable_ollama_cloud": true` in
`~/.ollama/server.json`); the log then reports `Ollama cloud disabled: true`. Pull the models while
online, then nothing needs the internet:

```bash
export OLLAMA_NO_CLOUD=1
export OLLAMA_CONTEXT_LENGTH=8192          # start small; the advertised maximum is not a free lunch on 36 GB
ollama pull qwen3.5:4b
ollama pull qwen3.5:9b                     # comparison only; not assumed better
ollama serve
```

Artifact sizes on the library page are download sizes, not peak RAM. Keep headroom for Logic and its
plug-ins and measure memory pressure with both models loaded in turn.

"No internet" and "no network interfaces" are different. The OSC channel advertises on a LAN address and
refuses loopback; the local profile does not use OSC, so it runs on a Mac with Wi-Fi off, but the full
profile's OSC tools do not.

## The client loop

MCP is not a model runtime. A small host process owns the loop; the model only emits tool calls.

```
text (typed, or a local ASR)            deterministic grammar handles the common phrases first
  -> Qwen via Ollama /api/chat           tools = tools/list of the local profile, converted 1:1
  -> each tool_call -> tools/call        the server validates, resolves, guards, writes, reads back
  -> append role:"tool" result           the model sees state/reason/fix, nothing else
  -> stop when no tool_calls             never re-send a write on "uncertain"
```

Converting the MCP tool list to Ollama's tool format is mechanical: each entry becomes
`{"type": "function", "function": {"name", "description", "parameters": inputSchema}}`, and a reply's
`message.tool_calls[*].function.{name, arguments}` maps straight onto `tools/call`. The host must not
add retries, fallbacks to another tool, or a cloud model when the local one fails: that is the
behaviour the profile is built to prevent. The gesture loop stays model-free as before.

## Acceptance gate before anyone calls this working

1. **Fixture half (done, in `tests/test_local_profile.py`, `tests/test_target_safety.py`,
   `tests/test_policy.py`):** forbidden tool names, invalid and non-finite arguments, duplicate and
   contradictory targets, stale readback, project switches, locked screen and dialogs, concurrent
   mutations, text inside track names that reads like an instruction. All refused or reported honestly,
   with nothing sent.
2. **Model half (not done):** 50 recorded English/Tanglish intents × {4B, 9B} × 3 repetitions against
   the fake adapters in `tests/fakeax.py`. Record schema validity, wrong-target and unauthorised-write
   counts, how "uncertain" is handled, median/p95 latency, peak RAM. The bar is zero unauthorised writes
   and zero wrong targets.
3. **Live half (not done):** a disposable project, the eight tools only, with a human watching. Passing
   fixtures is not evidence about Logic.

No fine-tuning until step 2 shows a repeatable model error.
