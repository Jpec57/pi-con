from __future__ import annotations

import contextlib
import logging
import math
import os
import queue
import threading
import time
from collections.abc import Callable, Iterator

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

from .config import Settings
from .debug import DebugState
from .events import EventBus
from .models import load_model
from .stt import Transcriber, Utterance

logger = logging.getLogger(__name__)

SCORE_PUBLISH_INTERVAL = 0.25  # seconds between live meter updates sent to the UI
STALL_SECONDS = 3.0  # no audio block for this long means the mic stream is stuck
MAX_STALL_STREAK = 4  # reopen attempts without any audio coming back before the process restarts itself
CLOSE_TIMEOUT = 5.0  # closing a wedged USB stream can hang; give up and restart the process
WAV_LOOP_PAUSE = 3.0  # seconds of silence between replays of the --wav file


def to_model_rate(block: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Resample one int16 mono block (e.g. 48 kHz from a USB mic) to the rate the wake word model needs."""
    divisor = math.gcd(source_rate, target_rate)
    resampled = resample_poly(block.astype(np.float32), target_rate // divisor, source_rate // divisor)
    return np.clip(resampled, -32768, 32767).astype(np.int16)


class WakeWordListener:
    def __init__(
        self,
        settings: Settings,
        on_wake: Callable[[], None],
        bus: EventBus | None = None,
        wav_frames: list[np.ndarray] | None = None,
        transcriber: Transcriber | None = None,
        debug: DebugState | None = None,
    ) -> None:
        self.settings = settings
        self.on_wake = on_wake
        self.bus = bus or EventBus()
        self.wav_frames = wav_frames
        self.model = load_model(settings.wakeword)
        self.frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=8)
        self.capture_rate = settings.sample_rate
        self.debug = debug or DebugState()
        self._stream: sd.InputStream | None = None
        self._stall_streak = 0
        self._restart_audio = threading.Event()
        self.transcriber = transcriber
        self._listen_requested = threading.Event()

    def request_audio_restart(self) -> bool:
        """Ask the loop to close and reopen the microphone (used by the UI)."""
        self._restart_audio.set()
        return True

    def request_listen(self) -> bool:
        """Start transcribing now, as if the wake word had just been heard (used by the UI button)."""
        if self.transcriber is None:
            return False
        self._listen_requested.set()
        return True

    def _capture(self, indata: np.ndarray, _frames: int, _time: object, status: sd.CallbackFlags) -> None:
        self.debug.frames += 1
        self.debug.last_frame = time.monotonic()
        if status:
            if status.input_overflow:
                self.debug.overflows += 1
            logger.debug("Audio status: %s", status)
        try:
            self.frames.put_nowait(indata[:, 0].copy())
        except queue.Full:
            logger.warning("Audio queue full; dropping frame")

    def _replay_wav(self) -> None:
        """Feed the file in real time, forever, so it can be tested without a microphone."""
        frame_seconds = self.settings.frame_samples / self.settings.sample_rate
        silence = np.zeros(self.settings.frame_samples, dtype=np.int16)
        while True:
            for frame in [*self.wav_frames, *[silence] * int(WAV_LOOP_PAUSE / frame_seconds)]:
                self.frames.put(frame)
                time.sleep(frame_seconds)

    @contextlib.contextmanager
    def _audio_source(self) -> Iterator[None]:
        if self.wav_frames is not None:
            threading.Thread(target=self._replay_wav, name="wav-replay", daemon=True).start()
            yield
            return
        self._start_stream()
        try:
            yield
        finally:
            self._close_stream()

    def _start_stream(self) -> None:
        stream = self._open_stream()
        stream.start()
        self._stream = stream
        self.debug.last_frame = time.monotonic()
        self.debug.stream = "running"
        try:
            self.debug.device = str(sd.query_devices(self.settings.audio_device, "input")["name"])
        except Exception:  # informational only
            self.debug.device = str(self.settings.audio_device)
        self.debug.capture_rate = self.capture_rate
        logger.info("Audio input open: %s at %d Hz", self.debug.device, self.capture_rate)

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.abort()
            stream.close()
        except Exception:  # a dead device can raise anything; we only want it gone
            logger.exception("Error while closing the audio input")

    def _reopen_stream(self, reason: str) -> None:
        """Close and reopen the mic. If that hangs or keeps failing, restart the whole process (systemd revives it)."""
        self.debug.stream_restarts += 1
        self.debug.stream = "restarting"
        logger.warning("Restarting the audio input: %s", reason)
        self.bus.publish("status", state="info", message=f"Restarting the audio input ({reason})")
        closer = threading.Thread(target=self._close_stream, name="close-audio", daemon=True)
        closer.start()
        closer.join(CLOSE_TIMEOUT)
        if closer.is_alive():
            logger.critical("Closing the audio input hung; restarting the process")
            os._exit(3)
        while not self.frames.empty():
            self.frames.get_nowait()
        try:
            self._start_stream()
        except Exception as error:
            self.debug.stream = "failed"
            self.debug.last_error = str(error)
            logger.error("Cannot reopen the audio input: %s", error)

    def _check_audio(self, now: float) -> None:
        """Called when no block arrived for a second: restart the mic if it has been silent too long."""
        if self.wav_frames is not None:
            return
        if self._restart_audio.is_set() or self._stream is None:
            self._restart_audio.clear()
            self._reopen_stream("requested")
        elif time.monotonic() - self.debug.last_frame >= STALL_SECONDS:
            self.debug.stalls += 1
            self._stall_streak += 1
            if self._stall_streak > MAX_STALL_STREAK:
                logger.critical("Audio still stuck after %d restarts; restarting the process", MAX_STALL_STREAK)
                os._exit(3)
            self.debug.last_error = f"no audio for {STALL_SECONDS:.0f}s"
            self._reopen_stream(self.debug.last_error)

    def _open_stream(self) -> sd.InputStream:
        """Open the mic at 16 kHz if it supports it, else at its native rate (USB mics are often 44.1/48 kHz only)."""
        wanted = self.settings.sample_rate
        try:
            return self._input_stream(wanted)
        except sd.PortAudioError as error:
            native = int(sd.query_devices(self.settings.audio_device, "input")["default_samplerate"])
            if native == wanted:
                raise
            logger.info("Microphone cannot record at %d Hz (%s); recording at %d Hz and resampling", wanted, error, native)
            return self._input_stream(native)

    def _input_stream(self, rate: int) -> sd.InputStream:
        self.capture_rate = rate
        return sd.InputStream(
            samplerate=rate,
            blocksize=self.settings.frame_samples * rate // self.settings.sample_rate,
            device=self.settings.audio_device,
            channels=1,
            dtype="int16",
            callback=self._capture,
        )

    def _announce_capture(self, active: bool) -> None:
        self.bus.publish("capture", persist=False, active=active)

    def run_forever(self) -> None:
        source = "wav file replay" if self.wav_frames is not None else "microphone"
        message = f"Listening for '{self.settings.wakeword}' (threshold {self.settings.threshold:.2f}, {source})"
        logger.info(message)
        with self._audio_source():
            self.bus.publish("status", state="listening", message=message)
            last_wake = float("-inf")
            last_publish = 0.0
            peak_score = peak_level = 0.0
            utterance: Utterance | None = None
            utterance_started = 0.0
            last_text = ""
            while True:
                try:
                    frame = self.frames.get(timeout=1.0)
                except queue.Empty:
                    self._check_audio(time.monotonic())
                    continue
                self._stall_streak = 0
                if self._restart_audio.is_set():
                    self._check_audio(time.monotonic())
                    continue
                if self.capture_rate != self.settings.sample_rate:  # resample here, not in the audio callback
                    frame = to_model_rate(frame, self.capture_rate, self.settings.sample_rate)
                now = time.monotonic()
                if self.transcriber is not None and utterance is None and self._listen_requested.is_set():
                    utterance, utterance_started, last_text = self.transcriber.start(), now, ""
                    self._announce_capture(True)
                self._listen_requested.clear()
                if utterance is not None:
                    text, finished = utterance.accept(frame)
                    if text != last_text:
                        last_text = text
                        self.bus.publish("partial", persist=False, text=text)
                    if finished or now - utterance_started >= self.settings.max_listen_seconds:
                        if last_text:
                            logger.info("Heard: %s", last_text)
                            self.bus.publish("transcript", text=last_text)
                        else:
                            logger.info("Nothing heard")
                        utterance = None
                        self._announce_capture(False)
                        self.model.reset()
                    continue
                scores = self.model.predict(frame)
                score = float(scores.get(self.settings.wakeword, 0.0))  # the model returns numpy floats
                if score >= 0.1:
                    logger.debug("score %.3f", score)
                now = time.monotonic()

                peak_score = max(peak_score, score)
                peak_level = max(peak_level, float(np.sqrt(np.mean(np.square(frame, dtype=np.float32)))) / 32768)
                if now - last_publish >= SCORE_PUBLISH_INTERVAL:
                    self.bus.publish(
                        "score", persist=False, score=round(peak_score, 3), level=round(peak_level, 4),
                        threshold=self.settings.threshold,
                    )
                    last_publish, peak_score, peak_level = now, 0.0, 0.0

                if score >= self.settings.threshold and now - last_wake >= self.settings.cooldown_seconds:
                    logger.info("Wake word detected (score %.3f)", score)
                    last_wake = now
                    self.bus.publish("wake", word=self.settings.wakeword, score=round(score, 3))
                    self.on_wake()
                    if self.transcriber is not None:
                        self._listen_requested.set()
                    # Drop the audio that already triggered so it can't re-trigger.
                    self.model.reset()
