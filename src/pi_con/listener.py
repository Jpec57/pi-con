from __future__ import annotations

import logging
import queue
from collections.abc import Callable

import numpy as np
import sounddevice as sd
from openwakeword.model import Model

from .config import Settings

logger = logging.getLogger(__name__)


class WakeWordListener:
    def __init__(self, settings: Settings, on_wake: Callable[[], None]) -> None:
        self.settings = settings
        self.on_wake = on_wake
        self.model = Model()
        self.frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=8)

    def _capture(self, indata: np.ndarray, _frames: int, _time: object, status: sd.CallbackFlags) -> None:
        if status:
            logger.warning("Audio status: %s", status)
        try:
            self.frames.put_nowait(indata[:, 0].copy())
        except queue.Full:
            logger.warning("Audio queue full; dropping frame")

    def run_forever(self) -> None:
        logger.info("Listening for '%s' (threshold %.2f)", self.settings.wakeword, self.settings.threshold)
        with sd.InputStream(
            samplerate=self.settings.sample_rate,
            blocksize=self.settings.frame_samples,
            device=self.settings.audio_device,
            channels=1,
            dtype="int16",
            callback=self._capture,
        ):
            while True:
                scores = self.model.predict(self.frames.get())
                score = scores.get(self.settings.wakeword, 0.0)
                if score >= self.settings.threshold:
                    logger.info("Wake word detected (score %.3f)", score)
                    self.on_wake()
