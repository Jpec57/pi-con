from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator

import numpy as np
import sounddevice as sd

from .config import Settings
from .events import EventBus
from .models import load_model

logger = logging.getLogger(__name__)

SCORE_PUBLISH_INTERVAL = 0.25  # seconds between live meter updates sent to the UI
WAV_LOOP_PAUSE = 3.0  # seconds of silence between replays of the --wav file


class WakeWordListener:
    def __init__(
        self,
        settings: Settings,
        on_wake: Callable[[], None],
        bus: EventBus | None = None,
        wav_frames: list[np.ndarray] | None = None,
    ) -> None:
        self.settings = settings
        self.on_wake = on_wake
        self.bus = bus or EventBus()
        self.wav_frames = wav_frames
        self.model = load_model(settings.wakeword)
        self.frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=8)

    def _capture(self, indata: np.ndarray, _frames: int, _time: object, status: sd.CallbackFlags) -> None:
        if status:
            logger.warning("Audio status: %s", status)
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
        with sd.InputStream(
            samplerate=self.settings.sample_rate,
            blocksize=self.settings.frame_samples,
            device=self.settings.audio_device,
            channels=1,
            dtype="int16",
            callback=self._capture,
        ):
            yield

    def run_forever(self) -> None:
        source = "wav file replay" if self.wav_frames is not None else "microphone"
        message = f"Listening for '{self.settings.wakeword}' (threshold {self.settings.threshold:.2f}, {source})"
        logger.info(message)
        with self._audio_source():
            self.bus.publish("status", state="listening", message=message)
            last_wake = float("-inf")
            last_publish = 0.0
            peak_score = peak_level = 0.0
            while True:
                frame = self.frames.get()
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
                    # Drop the audio that already triggered so it can't re-trigger.
                    self.model.reset()
