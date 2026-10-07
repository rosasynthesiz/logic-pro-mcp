"""`voiced` — push-to-talk speech into `gestured`.

Streams recognised text to `gestured` as `P.Utterance` JSON lines over UDP `P.PORT_VOICE` — see
protocol.py for the wire format and GESTURE_VOICE_PLAN.md section 4 for why this exists at all instead
of just using `superwhisper`: that app types into the focused field, and this system needs the text as
an *event* its own grammar can parse, not as keystrokes landing wherever focus happens to be.

**Push-to-talk, never a wake word.** "Hey Logic" with music playing is a false-trigger machine — the
whole point of a wake word is picking a human voice out of silence, and a mix session is never silent.
A sustain pedal (or a held hotkey) has no such problem: it is either down or it isn't, and the hands
that would use it are usually already near a MIDI controller.

**Never act on an interim.** `Utterance.final=False` hypotheses exist so the HUD has something to show
while the pedal is still down; `gestured` only ever calls `grammar.parse()` on a `final=True` line (see
its own drain loop). voiced does not enforce that on the wire — it can't, UDP has no memory of what the
receiver does — so the discipline lives entirely in the consumer. Acting on an interim here would mean
"solo str" fires before the speaker finishes saying "strings".

Nothing above the `Transcriber`/`AudioSource`/`Trigger` boundary cares which backend is in play; `main()`
picks one, fails loudly if none is usable, and everything else just calls the small interface.
"""
from __future__ import annotations

import abc
import argparse
import contextlib
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import protocol as P

SAMPLE_RATE = 16000              # what every local ASR model on this Mac wants: mono, 16 kHz, float32.
SUSTAIN_CC = 64                  # MIDI sustain pedal controller number.
SUSTAIN_THRESHOLD = 64           # CC value >= this is "down" (MIDI convention: 0-63 up, 64-127 down).


# =============================================================================================
# transcription backends
# =============================================================================================

class Transcriber(abc.ABC):
    """A speech-to-text engine. `transcribe` is one blocking call over a whole audio buffer — neither
    backend below does true incremental streaming worth the added complexity, so `Session` gets its
    "interim" behaviour instead by re-running `transcribe` on the growing buffer every so often while
    the pedal is held (see `Session.pump`). That is slower per-call than real streaming would be, but a
    push-to-talk utterance is a couple of seconds of audio, not a continuous feed."""

    name: str = "transcriber"

    @abc.abstractmethod
    def transcribe(self, samples, sample_rate: int) -> str:
        """`samples`: 1-D float32 numpy array, values in -1..1. Returns the recognised text, "" for
        silence. Must raise, not return garbage, on a real failure — `Session` logs and moves on."""


class ParakeetTranscriber(Transcriber):
    """NVIDIA Parakeet via `parakeet-mlx` — the preferred backend on this Mac: MLX runs it on the GPU
    with no subprocess and no model-management step beyond the first download."""

    name = "parakeet-mlx"

    def __init__(self, model: str = "mlx-community/parakeet-tdt-0.6b-v2"):
        from parakeet_mlx import from_pretrained    # raises ImportError if the package isn't installed
        self._model = from_pretrained(model)

    def transcribe(self, samples, sample_rate: int) -> str:
        import numpy as np
        import soundfile as sf
        # The one entry point guaranteed stable across parakeet-mlx versions is "give it a file path".
        # Round-tripping a couple of seconds of audio through a temp WAV costs nothing next to the
        # model's own inference time.
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            sf.write(f.name, samples.astype(np.float32), sample_rate)
            result = self._model.transcribe(f.name)
        text = getattr(result, "text", None)
        return text if text is not None else str(result)


class WhisperCppTranscriber(Transcriber):
    """Shells out to a `whisper.cpp` build. Slower than Parakeet on Apple Silicon but works anywhere the
    binary and a GGML model file exist, which matters for the Tamil/Tanglish path this repo already has
    notes on (see personal-dictation-model.md) — Parakeet is English-only.

    whisper.cpp has renamed its CLI (`main` -> `whisper-cli`) and shuffled flags across releases; this
    targets a recent build with `-nt`/`--no-timestamps` for plain-text stdout. Pass the exact binary and
    model this machine has via `--whisper-binary`/`--whisper-model` if the defaults don't match.
    """

    name = "whisper.cpp"

    def __init__(self, binary: str, model: str):
        self.binary = binary
        self.model = model

    def transcribe(self, samples, sample_rate: int) -> str:
        import numpy as np
        import soundfile as sf
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            sf.write(f.name, samples.astype(np.float32), sample_rate)
            try:
                out = subprocess.run(
                    [self.binary, "-m", self.model, "-f", f.name, "-nt", "-np"],
                    capture_output=True, text=True, timeout=30, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                raise RuntimeError(f"whisper.cpp failed to run: {e}") from e
        return out.stdout.strip()


def find_transcriber(prefer: str = "auto", *, model: str = "mlx-community/parakeet-tdt-0.6b-v2",
                      whisper_binary: str | None = None, whisper_model: str | None = None) -> Transcriber:
    """Pick a backend in the order GESTURE_VOICE_PLAN.md section 4 calls out, or the one explicitly
    named by `prefer`. Never degrades silently: a voice daemon that starts up and stays mute is a much
    worse failure than one that refuses to start, because nothing downstream can tell those two apart
    except by noticing the mix session went quiet on it."""
    if prefer in ("auto", "parakeet"):
        try:
            return ParakeetTranscriber(model=model)
        except ImportError:
            if prefer == "parakeet":
                raise RuntimeError(
                    "parakeet-mlx is not installed. Install it with:\n"
                    "    uv pip install parakeet-mlx\n"
                    "(it's in this project's optional `voice` extra: `uv sync --extra voice`)"
                )

    if prefer in ("auto", "whispercpp"):
        binary = whisper_binary or shutil.which("whisper-cli") or shutil.which("main")
        if binary and whisper_model and Path(whisper_model).exists():
            return WhisperCppTranscriber(binary, whisper_model)
        if prefer == "whispercpp":
            raise RuntimeError(
                "whisper.cpp is not set up. Build it (https://github.com/ggml-org/whisper.cpp), "
                "download a ggml model, then run:\n"
                "    voiced --backend whispercpp --whisper-binary /path/to/whisper-cli "
                "--whisper-model /path/to/ggml-base.en.bin"
            )

    raise RuntimeError(
        "No local speech-to-text backend is available. Install one of:\n"
        "    uv pip install parakeet-mlx                    (preferred on Apple Silicon; "
        "`uv sync --extra voice`)\n"
        "    build whisper.cpp and pass --whisper-binary / --whisper-model\n"
        "voiced refuses to start rather than run mute."
    )


# =============================================================================================
# audio capture
# =============================================================================================

class AudioSource(abc.ABC):
    """One mono float32 stream at `SAMPLE_RATE`. `read` never blocks longer than `timeout`, so the main
    loop stays responsive to a pedal release even mid-chunk."""

    name: str = "audio"

    @abc.abstractmethod
    def start(self) -> None: ...

    @abc.abstractmethod
    def read(self, timeout: float = 0.1):
        """Next chunk of samples (1-D float32 numpy array), or None if nothing arrived within timeout."""

    @abc.abstractmethod
    def stop(self) -> None: ...


class SounddeviceSource(AudioSource):
    """`sounddevice` (PortAudio) — the preferred path: low latency, no subprocess."""

    name = "sounddevice"

    def __init__(self, device=None):
        import sounddevice as sd          # raises ImportError if not installed; caller decides the fallback
        self._sd = sd
        self.device = device
        self._q: "queue.Queue" = queue.Queue()
        self._stream = None

    def _callback(self, indata, frames, time_info, status) -> None:
        self._q.put(indata[:, 0].copy())

    def start(self) -> None:
        self._stream = self._sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                             device=self.device, callback=self._callback)
        self._stream.start()

    def read(self, timeout: float = 0.1):
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        if self._stream is not None:
            with contextlib.suppress(Exception):
                self._stream.stop()
                self._stream.close()
            self._stream = None
        with self._q.mutex:
            self._q.queue.clear()


class FfmpegSource(AudioSource):
    """Falls back to a raw `ffmpeg` subprocess over AVFoundation, for a machine with ffmpeg but no
    working `sounddevice` (PortAudio not built, or not installed at all)."""

    name = "ffmpeg"

    def __init__(self, device: str = ":0"):
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("neither `sounddevice` nor `ffmpeg` is available for audio capture")
        self.device = device
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._q: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()

    def start(self) -> None:
        self._stop.clear()
        self._proc = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-f", "avfoundation", "-i", self.device,
             "-ar", str(SAMPLE_RATE), "-ac", "1", "-f", "f32le", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        import numpy as np
        chunk_bytes = 1024 * 4     # 1024 float32 samples per read
        while not self._stop.is_set() and self._proc is not None and self._proc.stdout is not None:
            buf = self._proc.stdout.read(chunk_bytes)
            if not buf:
                break
            self._q.put(np.frombuffer(buf, dtype=np.float32).copy())

    def read(self, timeout: float = 0.1):
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None:
            self._proc.terminate()
            with contextlib.suppress(Exception):
                self._proc.wait(timeout=1.0)
            self._proc = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        with self._q.mutex:
            self._q.queue.clear()


def find_audio_source(device=None) -> AudioSource:
    """`sounddevice` first, `ffmpeg` if that import fails. Whichever wins, `main()` prints which one —
    silently falling back would make a missing PortAudio build look like a working one until the first
    session, which is the wrong moment to find out."""
    try:
        return SounddeviceSource(device)
    except ImportError:
        return FfmpegSource(device or ":0")


# =============================================================================================
# push-to-talk triggers
# =============================================================================================

class Trigger(abc.ABC):
    """Fires `on_open()`/`on_close()` around one push-to-talk hold. `run` blocks until `stop` is set and
    must be driven from its own thread — both implementations below own a blocking listen loop."""

    @abc.abstractmethod
    def run(self, on_open, on_close, stop: threading.Event) -> None: ...


class MidiPedalTrigger(Trigger):
    """A sustain pedal (CC 64) on a CoreMIDI input. Uses `rtmidi` directly, the same way
    `channels/midi.py` opens ports — this repo has exactly one way of talking to CoreMIDI, not a second
    one built out of `mido` just because `mido` happens to also be a dependency.
    """

    def __init__(self, port_substring: str | None = None):
        import rtmidi
        self._rtmidi = rtmidi
        self.port_substring = port_substring
        self._midi_in = None

    def _open(self):
        mi = self._rtmidi.MidiIn()
        ports = mi.get_ports()
        if not ports:
            raise RuntimeError("no CoreMIDI input ports found — connect a sustain pedal or controller")
        index = 0
        if self.port_substring:
            matches = [i for i, name in enumerate(ports) if self.port_substring.lower() in name.lower()]
            if not matches:
                raise RuntimeError(f"no MIDI input matches {self.port_substring!r}; ports: {ports}")
            index = matches[0]
        mi.open_port(index)
        mi.ignore_types(sysex=True, timing=True, active_sense=True)
        print(f"voiced: listening for CC{SUSTAIN_CC} (sustain) on {ports[index]!r}", file=sys.stderr)
        return mi

    def run(self, on_open, on_close, stop: threading.Event) -> None:
        self._midi_in = self._open()
        held = False

        def callback(event, _data=None) -> None:
            nonlocal held
            msg, _dt = event
            if len(msg) >= 3 and (msg[0] & 0xF0) == 0xB0 and msg[1] == SUSTAIN_CC:
                down = msg[2] >= SUSTAIN_THRESHOLD
                if down and not held:
                    held = True
                    on_open()
                elif not down and held:
                    held = False
                    on_close()

        self._midi_in.set_callback(callback)
        try:
            while not stop.is_set():
                time.sleep(0.05)
        finally:
            with contextlib.suppress(Exception):
                self._midi_in.close_port()


class KeyHoldTrigger(Trigger):
    """Hold a hotkey instead of a pedal, via a Quartz `CGEventTap`.

    This needs the process running voiced (or its parent Terminal/IDE) granted under System Settings ->
    Privacy & Security -> Accessibility. Without that grant `CGEventTapCreate` returns None and `run`
    raises with that instruction rather than silently sitting there never firing — and per this
    project's own notes on Accessibility grants going stale, re-granting the permission is not enough;
    the process has to be restarted afterwards.
    """

    def __init__(self, keycode: int = 49):    # 49 = space bar, see channels/keys.py's KEYCODES table
        self.keycode = keycode

    def run(self, on_open, on_close, stop: threading.Event) -> None:
        import Quartz as Q

        held = False

        def handler(_proxy, event_type, event, _refcon):
            nonlocal held
            if event_type in (Q.kCGEventKeyDown, Q.kCGEventKeyUp):
                code = Q.CGEventGetIntegerValueField(event, Q.kCGKeyboardEventKeycode)
                if code == self.keycode:
                    down = event_type == Q.kCGEventKeyDown
                    if down and not held:
                        held = True
                        on_open()
                    elif not down and held:
                        held = False
                        on_close()
            return event

        mask = (1 << Q.kCGEventKeyDown) | (1 << Q.kCGEventKeyUp)
        tap = Q.CGEventTapCreate(Q.kCGSessionEventTap, Q.kCGHeadInsertEventTap,
                                  Q.kCGEventTapOptionListenOnly, mask, handler, None)
        if tap is None:
            raise RuntimeError(
                "could not create a CGEventTap — grant Accessibility to this terminal/Python under "
                "System Settings -> Privacy & Security -> Accessibility, then RESTART voiced "
                "(restarting just the permission does nothing; the process has to relaunch)"
            )
        source = Q.CFMachPortCreateRunLoopSource(None, tap, 0)
        loop = Q.CFRunLoopGetCurrent()
        Q.CFRunLoopAddSource(loop, source, Q.kCFRunLoopCommonModes)
        Q.CGEventTapEnable(tap, True)
        try:
            while not stop.is_set():
                Q.CFRunLoopRunInMode(Q.kCFRunLoopDefaultMode, 0.1, False)
        finally:
            Q.CGEventTapEnable(tap, False)
            Q.CFRunLoopRemoveSource(loop, source, Q.kCFRunLoopCommonModes)


# =============================================================================================
# the session: one push-to-talk hold
# =============================================================================================

class Session:
    """One push-to-talk hold: accumulates audio while open, re-transcribes the growing buffer every
    `interim_every` seconds and sends the result as `final=False`, then sends exactly one `final=True`
    line when the trigger closes.

    `gestured` must never act on a `final=False` line — see this module's docstring and `P.Utterance`'s
    own. voiced can't enforce that from here; it can only make sure it never mislabels a provisional
    result as final.
    """

    def __init__(self, transcriber: Transcriber, source: AudioSource, sender: P.UdpSender,
                 interim_every: float = 0.6):
        self.transcriber = transcriber
        self.source = source
        self.sender = sender
        self.interim_every = interim_every
        self._buf: list = []
        self._last_interim = 0.0

    def open(self) -> None:
        self._buf = []
        self._last_interim = 0.0
        self.source.start()
        print("voiced: mic hot", file=sys.stderr)

    def pump(self) -> None:
        """Call this often while open. Drains whatever audio has arrived and, no more often than
        `interim_every`, re-runs the transcriber over everything captured so far and emits an interim."""
        chunk = self.source.read(timeout=0.05)
        if chunk is not None:
            self._buf.append(chunk)
        now = time.monotonic()
        if self._buf and (now - self._last_interim) >= self.interim_every:
            self._last_interim = now
            self._emit(final=False)

    def close(self) -> None:
        self.source.stop()
        self._emit(final=True)
        print("voiced: mic closed", file=sys.stderr)

    def _emit(self, final: bool) -> None:
        import numpy as np
        text = ""
        if self._buf:
            samples = np.concatenate(self._buf)
            try:
                text = self.transcriber.transcribe(samples, SAMPLE_RATE).strip()
            except Exception as e:                          # a bad frame must never kill the daemon
                print(f"voiced: transcription failed: {e}", file=sys.stderr)
                text = ""
        self.sender.send(P.Utterance(t=P.now(), text=text, final=final).to_json())
        print(f"voiced: {'FINAL' if final else '...  '} {text!r}", file=sys.stderr)


def run_dry_run(sender: P.UdpSender) -> None:
    """Reads lines from stdin and emits each as a `final=True` utterance, no microphone or trigger
    involved. This is what makes the rest of the pipeline (grammar, arbiter, gestured) testable with no
    audio hardware in the loop at all."""
    print("voiced --dry-run: type an utterance per line, Ctrl-D to stop", file=sys.stderr)
    for line in sys.stdin:
        text = line.rstrip("\n")
        if not text:
            continue
        sender.send(P.Utterance(t=P.now(), text=text, final=True).to_json())
        print(f"voiced: FINAL {text!r}", file=sys.stderr)


def run_live(trigger: Trigger, transcriber: Transcriber, source: AudioSource, port: int) -> None:
    """The live loop. Trigger open/close events are pushed onto a queue from the trigger's own thread
    (a MIDI callback, or a CFRunLoop) and drained here on the main thread, so `Session` — which is not
    designed to be thread-safe — only ever runs from one place."""
    sender = P.sender(port)
    session = Session(transcriber, source, sender)
    stop = threading.Event()
    events: "queue.Queue[str]" = queue.Queue()

    trigger_thread = threading.Thread(
        target=trigger.run, args=(lambda: events.put("open"), lambda: events.put("close"), stop),
        daemon=True,
    )
    trigger_thread.start()

    def handle_sigint(signum, frame) -> None:
        stop.set()

    signal.signal(signal.SIGINT, handle_sigint)

    active = False
    try:
        while not stop.is_set():
            try:
                ev = events.get(timeout=0.05)
            except queue.Empty:
                ev = None
            if ev == "open" and not active:
                session.open()
                active = True
            elif ev == "close" and active:
                session.close()
                active = False
            if active:
                session.pump()
    finally:
        if active:
            session.close()
        sender.close()
        trigger_thread.join(timeout=1.0)


def main() -> None:
    ap = argparse.ArgumentParser(description="voiced — push-to-talk speech into gestured")
    ap.add_argument("--trigger", choices=("midi", "key"), default="midi",
                     help="midi = sustain pedal CC64 (default); key = hold a hotkey")
    ap.add_argument("--midi-port", help="substring to match a CoreMIDI input name (default: first port)")
    ap.add_argument("--keycode", type=int, default=49,
                     help="virtual keycode to hold for --trigger key (default 49 = space)")
    ap.add_argument("--backend", choices=("auto", "parakeet", "whispercpp"), default="auto")
    ap.add_argument("--parakeet-model", default="mlx-community/parakeet-tdt-0.6b-v2")
    ap.add_argument("--whisper-binary", help="path to a whisper.cpp whisper-cli/main binary")
    ap.add_argument("--whisper-model", help="path to a ggml .bin model for whisper.cpp")
    ap.add_argument("--audio-device", help="sounddevice index/name, or ffmpeg avfoundation index (':0')")
    ap.add_argument("--port", type=int, default=P.PORT_VOICE)
    ap.add_argument("--dry-run", action="store_true",
                     help="read utterances from stdin instead of a microphone; no ASR, no trigger")
    args = ap.parse_args()

    if args.dry_run:
        sender = P.sender(args.port)
        try:
            run_dry_run(sender)
        finally:
            sender.close()
        return

    try:
        transcriber = find_transcriber(args.backend, model=args.parakeet_model,
                                        whisper_binary=args.whisper_binary,
                                        whisper_model=args.whisper_model)
    except RuntimeError as e:
        print(f"voiced: {e}", file=sys.stderr)
        raise SystemExit(2)
    print(f"voiced: transcriber = {transcriber.name}", file=sys.stderr)

    try:
        source = find_audio_source(args.audio_device)
    except RuntimeError as e:
        print(f"voiced: {e}", file=sys.stderr)
        raise SystemExit(2)
    print(f"voiced: audio capture = {source.name}", file=sys.stderr)

    if args.trigger == "midi":
        trigger: Trigger = MidiPedalTrigger(args.midi_port)
    else:
        trigger = KeyHoldTrigger(args.keycode)
        print("voiced: --trigger key needs Accessibility access for this process "
              "(System Settings -> Privacy & Security -> Accessibility)", file=sys.stderr)

    print(f"voiced: trigger = {args.trigger} -> udp/{args.port}", file=sys.stderr)
    try:
        run_live(trigger, transcriber, source, args.port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
