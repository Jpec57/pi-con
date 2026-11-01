"""Spoken replies: French text-to-speech with Piper (local, free), played on the Pi's speaker.

`Speaker` listens on the event bus: `ai_reply` (an AI answer) and `say` (a sentence a skill wants spoken) are read aloud
while the option is on. While it talks, `Speaker.busy()` is true so the listener ignores the microphone: the cheap USB
mic has no echo cancellation and would otherwise hear the assistant and wake itself up.
"""
from __future__ import annotations

import json
import logging
import math
import queue
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .events import EventBus

logger = logging.getLogger(__name__)

DEFAULT_VOICE = "fr_FR-siwis-medium"
TAIL_SECONDS = 0.4  # keep the microphone muted a little after the speaker stops (room echo)
MAX_SPOKEN_CHARS = 600  # a long answer is cut at a sentence end: nobody wants a minute-long monologue
QUEUE_SIZE = 4

Synthesize = Callable[[str], tuple[np.ndarray, int]]  # text -> (mono int16 samples, sample rate)
Play = Callable[[np.ndarray, int, "str | int | None"], None]  # blocks until the sound has been played


def clean_for_speech(text: str) -> str:
    """Remove what sounds wrong when read aloud (markdown, links, emoji-like symbols) and cap the length."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [label](url) -> label
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[*_`#>~|]+", " ", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    text = re.sub(r"[^\w\s.,;:!?'’«»()%€$°/+-]", " ", text)  # emoji and other symbols
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_SPOKEN_CHARS:
        cut = text[:MAX_SPOKEN_CHARS]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[:end + 1] if end > 0 else cut
    return text


def voice_files(voice_dir: str | Path, voice: str) -> tuple[Path, Path]:
    folder = Path(voice_dir)
    return folder / f"{voice}.onnx", folder / f"{voice}.onnx.json"


def ensure_voice(voice_dir: str | Path, voice: str = DEFAULT_VOICE) -> Path:
    """Download the Piper voice (~60 MB for a medium one) into `voice_dir` unless it is already there."""
    model, config = voice_files(voice_dir, voice)
    if model.is_file() and config.is_file():
        return model
    from piper.download_voices import download_voice

    logger.info("Downloading the Piper voice %s to %s", voice, voice_dir)
    Path(voice_dir).mkdir(parents=True, exist_ok=True)
    download_voice(voice, Path(voice_dir))
    if not (model.is_file() and config.is_file()):
        raise RuntimeError(f"Piper voice download incomplete: {voice}")
    return model


class PiperSynthesizer:
    """Loads the voice on first use, so the option costs no memory or startup time while it stays off."""

    def __init__(self, voice_dir: str | Path, voice: str = DEFAULT_VOICE) -> None:
        self.voice_dir, self.voice_name = voice_dir, voice
        self._voice = None
        self._lock = threading.Lock()

    def __call__(self, text: str) -> tuple[np.ndarray, int]:
        with self._lock:
            if self._voice is None:
                from piper import PiperVoice

                logger.info("Loading the Piper voice %s", self.voice_name)
                self._voice = PiperVoice.load(ensure_voice(self.voice_dir, self.voice_name))
            chunks = list(self._voice.synthesize(text))
        if not chunks:
            return np.zeros(0, dtype=np.int16), 22_050
        return np.concatenate([chunk.audio_int16_array for chunk in chunks]), chunks[0].sample_rate


def play_sound(audio: np.ndarray, rate: int, device: str | int | None) -> None:
    """Play on the output device, resampled to the rate the device supports (USB speakers are often 44.1/48 kHz only)."""
    import sounddevice as sd
    from scipy.signal import resample_poly

    target = int(sd.query_devices(device, "output")["default_samplerate"])
    if target != rate:
        divisor = math.gcd(rate, target)
        audio = np.clip(resample_poly(audio.astype(np.float32), target // divisor, rate // divisor), -32768, 32767).astype(np.int16)
    sd.play(audio, target, device=device)
    sd.wait()


def stop_sound() -> None:
    import sounddevice as sd

    sd.stop()


class Speaker:
    """The "spoken replies" option: on/off state (saved in `.state/tts.json`), a queue and the playback thread."""

    def __init__(
        self,
        bus: EventBus,
        synthesize: Synthesize,
        play: Play = play_sound,
        stop_playback: Callable[[], None] = stop_sound,
        enabled: bool = False,
        output_device: str | int | None = None,
        path: str | Path = ".state/tts.json",
        voice: str = DEFAULT_VOICE,
        after_reply: Callable[[], None] | None = None,
    ) -> None:
        self.bus = bus
        self.synthesize = synthesize
        self.play = play
        self.stop_playback = stop_playback
        self.output_device = output_device
        self.voice = voice
        self.path = Path(path)
        self.enabled = enabled
        self.after_reply = after_reply  # called once an AI answer was read (or at once if it will not be): open the follow-up window
        self._load()
        self._queue: queue.Queue[tuple[int, str, bool]] = queue.Queue(maxsize=QUEUE_SIZE)
        self._pending = 0  # sentences queued or being played: the microphone stays muted for all of them
        self._speaking = False
        self._quiet_until = 0.0
        self._generation = 0  # bumped by stop(): sentences queued before it are dropped
        self._lock = threading.Lock()

    # ---- state ------------------------------------------------------------------------------------------------
    def snapshot(self) -> dict:
        return {"enabled": self.enabled, "voice": self.voice}

    def announce(self) -> None:
        self.bus.publish("tts", **self.snapshot())

    def set_enabled(self, on: bool) -> None:
        self.enabled = on
        if not on:
            self.stop()
        self._save()
        self.announce()

    def busy(self) -> bool:
        """True while speaking or just after: the microphone must not listen."""
        return self._pending > 0 or self._speaking or time.monotonic() < self._quiet_until

    # ---- speaking ---------------------------------------------------------------------------------------------
    def say(self, text: str, force: bool = False, followup: bool = False) -> bool:
        """Queue a sentence. Ignored while the option is off (unless `force`, used by the test button)."""
        if not (self.enabled or force):
            return False
        spoken = clean_for_speech(text)
        if not spoken:
            return False
        with self._lock:
            self._pending += 1  # before queueing, so the microphone is muted from this very moment
        try:
            self._queue.put_nowait((self._generation, spoken, followup))
        except queue.Full:
            self._done()
            logger.warning("Spoken replies are backed up, dropping %r", spoken[:40])
            return False
        return True

    def _done(self) -> None:
        with self._lock:
            self._pending = max(0, self._pending - 1)

    def stop(self) -> None:
        """Cut the sentence being played and forget the queued ones."""
        with self._lock:
            self._generation += 1
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            self._done()
        if self._speaking:
            try:
                self.stop_playback()
            except Exception:
                logger.exception("Cannot stop the playback")

    def _speak_next(self, timeout: float | None = None) -> bool:
        """Play one queued sentence. Returns False if the queue stayed empty."""
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return False
        generation, text, followup = item
        if generation != self._generation:
            self._done()
            return True
        self._speaking = True
        self.bus.publish("speaking", persist=False, active=True)
        try:
            audio, rate = self.synthesize(text)
            if len(audio) and generation == self._generation:
                self.play(audio, rate, self.output_device)
        except ImportError:
            logger.error("Piper is not installed: pip install piper-tts")
            self.bus.publish("status", state="info", message="Voix: piper-tts n'est pas installé")
        except Exception as error:
            logger.exception("Cannot speak")
            self.bus.publish("status", state="info", message=f"Voix: {error}")
        finally:
            self._quiet_until = time.monotonic() + TAIL_SECONDS
            self._speaking = False
            self._done()
            self.bus.publish("speaking", persist=False, active=False)
        if followup and generation == self._generation and self.after_reply:
            self.after_reply()
        return True

    def start(self) -> None:
        """Start the playback thread and read the AI answers / skill sentences published on the bus."""
        threading.Thread(target=self._play_loop, name="tts-play", daemon=True).start()
        subscriber, _ = self.bus.subscribe(after_id=1 << 62)  # live events only: never read old answers

        def listen() -> None:
            while True:
                event = subscriber.get()
                if event["type"] == "say" and event.get("text"):
                    self.say(event["text"])
                elif event["type"] == "ai_reply":
                    if not (event.get("text") and self.say(event["text"], followup=True)) and self.after_reply:
                        self.after_reply()  # not read aloud (voice off): the follow-up window opens right away

        threading.Thread(target=listen, name="tts-events", daemon=True).start()

    def _play_loop(self) -> None:
        while True:
            self._speak_next(timeout=1.0)

    # ---- persistence ------------------------------------------------------------------------------------------
    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"enabled": self.enabled}), encoding="utf-8")
        except OSError:
            logger.warning("Cannot save the spoken replies option to %s", self.path)

    def _load(self) -> None:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(saved.get("enabled"), bool):
            self.enabled = saved["enabled"]  # what was chosen in the UI wins over settings.json
