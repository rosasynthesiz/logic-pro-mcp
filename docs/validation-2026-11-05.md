# Safety validation follow-up

PR #1 head reviewed: fc69dcd03475ae1b1e1e8ed4763de9761b30da0f.

The draft predates current main and conflicted in server imports and tool decorators. This branch merges main, retaining the inspection-only runtime refusal outside every mutation guard. It preserves main platform dependency markers and inspection CI.

Found and corrected:
- MutationGuard silently proceeded without a process lock when opening the lock failed. It now refuses writes, including symlink, foreign-owner, shared-permission and nonregular lock files; unexpected flock errors refuse immediately. Private lock-file requirement means an old permissive lock must be removed by its owner while all MCP processes are stopped.
- logicpadd created a socket with ambient permissions and could unlink an arbitrary existing file. It now restricts the bound socket to 0600 and refuses non-socket or foreign-owner paths. Same-user clients are still trusted. Loopback UDP is not an authentication boundary and remains a release review item.

Validation limits:
- Windows host, no live Logic/macOS host. Initial frozen install of the old draft failed building its unconditional PyObjC dependency. Current main platform markers are preserved by the merge.
- Docker daemon unavailable. Ollama installed but no local models. No new real-model evaluation was run.
- Historical macOS checks on PR #1 succeeded. Those do not validate this branch.
- CI now includes the 50-intent x 3 oracle replay. Oracle tests host plumbing, not a model. Consult this branch CI for actual results.
- Recorded qwen3.5:4b and :9b model evaluations both failed the zero-unauthorized-write gate; description fixes still require remeasurement. Do not merge PR #1 or claim unattended model safety until these gates are met.
- No release published; signed installer, clean Mac installation, upgrade/uninstall, live AX/MCU and disposable-project qualification remain open.


## Independent review follow-up

- Local inspection profile previously imported native adapters and health could run diagnostics. It now imports only the schema server/result helpers in inspection mode, marks the instructions, and refuses both dispatcher and direct registered tool calls. The stdio inspection check covers CLI flag, environment selector, local console entry point and module entry point (all eight tools), with native/backend import assertions.
- Compact writes now acquire the mutation guard before preflight and hold it through step/thrash validation, write, readback and history updates. Deterministic two-thread gain and mute tests force the second writer to attempt entry while the first is paused, then require refusal based on the first result.
- A responding or silent listener is never stale merely because ping fails. Only connection-refused or missing-path errors allow stale recovery; other errors preserve the path. Startup rechecks identity before stale unlink. Shutdown only unlinks the originally bound socket inode; replacement files, symlinks and active sockets are preserved by regressions. These identity checks do not claim protection from malicious same-user filesystem races.
- Previous head e3e9f39 passed hosted macOS tests and its 150-case oracle replay. Follow-up changes require their own exact-head CI; historical success is not a substitute.
