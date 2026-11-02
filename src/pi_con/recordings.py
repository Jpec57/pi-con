"""Keep the audio the speech-to-text received, so the owner can hear it back and judge the microphone.

Each recording is a 16 kHz mono WAV (exactly what the recogniser got, after resampling) plus a small JSON file with what
was understood and the levels. Only the last `keep` are kept. They stay on the Pi, but the UI has no login: anyone on the
LAN can play them, so `recordings_keep: 0` in settings.json turns this off.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import wave
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

RATE = 16_000
MAX_KEEP = 5  # audio piles up fast: never keep more than this many, whatever the setting says
ID_PATTERN = re.compile(r"\d{8}-\d{6}-\d{3}")
FRAME = 1_280  # 80 ms: the same slices the wake word model sees


def levels(samples: np.ndarray) -> dict:
    """Peak, speech and noise-floor levels in dBFS (0 = digital full scale) and what they suggest about the microphone."""
    if len(samples) == 0:
        return {"peak_db": None, "speech_db": None, "noise_db": None, "warning": "empty"}
    audio = samples.astype(np.float32) / 32768
    peak = float(np.max(np.abs(audio)))
    usable = len(audio) // FRAME * FRAME
    frames = audio[:usable].reshape(-1, FRAME) if usable else audio.reshape(1, -1)
    rms = np.sqrt(np.mean(np.square(frames), axis=1)) + 1e-9
    db = 20 * np.log10(rms)
    # Voice = the loudest 5 % of the 80 ms frames (at least 3). A percentile would land on silence when the clip is mostly
    # silence, e.g. a 2 s sentence inside a 10 s follow-up window, and wrongly call a good microphone noisy.
    loudest = np.sort(db)[-max(3, int(len(db) * 0.05)):]
    speech, noise = float(np.mean(loudest)), float(np.percentile(db, 10))
    warning = ""
    if float(np.mean(np.abs(audio) >= 0.99)) > 0.002:  # a lone peak is fine; many flattened samples means it is saturated
        warning = "clipping: the signal is saturated (too loud or too much gain)"
    elif speech < -45:
        warning = "very quiet: speak closer to the microphone or raise its gain"
    elif speech - noise < 15 and len(frames) > 5:
        warning = "noisy: the voice is barely above the background noise"
    to_db = lambda value: round(20 * float(np.log10(max(value, 1e-9))), 1)  # noqa: E731
    return {"peak_db": to_db(peak), "speech_db": round(speech, 1), "noise_db": round(noise, 1), "warning": warning}


class RecordingStore:
    def __init__(self, folder: str | Path = ".state/recordings", keep: int = 10) -> None:
        self.folder = Path(folder)
        self.keep = max(0, min(int(keep), MAX_KEEP))
        self._lock = threading.Lock()

    def add(self, samples: np.ndarray, text: str = "", local: str = "", kind: str = "speech") -> dict:
        """Save one recording (int16 mono at 16 kHz) and return its description."""
        with self._lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            now = time.time()
            stamp = datetime.fromtimestamp(now)
            rec_id = stamp.strftime("%Y%m%d-%H%M%S-") + f"{stamp.microsecond // 1000:03d}"
            existing = [path.stem for path in self.folder.iterdir()
                        if path.suffix in (".wav", ".json") and ID_PATTERN.fullmatch(path.stem)]
            if existing and rec_id <= (latest := max(existing)):
                stamp = datetime.strptime(latest, "%Y%m%d-%H%M%S-%f") + timedelta(milliseconds=1)
                rec_id = stamp.strftime("%Y%m%d-%H%M%S-") + f"{stamp.microsecond // 1000:03d}"
            meta = {
                "audio": rec_id, "time": now, "kind": kind, "seconds": round(len(samples) / RATE, 1),
                "text": text, "local": local, **levels(samples),
            }
            with wave.open(str(self.folder / f"{rec_id}.wav"), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(RATE)
                out.writeframes(samples.astype(np.int16).tobytes())
            (self.folder / f"{rec_id}.json").write_text(json.dumps(meta), encoding="utf-8")
            self._prune()
        return meta

    def _prune(self) -> None:
        for old in sorted(self.folder.glob("*.wav"))[: -max(self.keep, 1)]:
            old.unlink(missing_ok=True)
            old.with_suffix(".json").unlink(missing_ok=True)

    def list(self) -> list[dict]:
        """Newest first."""
        found = []
        for path in sorted(self.folder.glob("*.json"), reverse=True):
            try:
                found.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return found[: self.keep]

    def wav(self, rec_id: str) -> Path | None:
        """Path of a recording, or None. The id is checked so a URL cannot reach any other file."""
        if not ID_PATTERN.fullmatch(rec_id):
            return None
        path = self.folder / f"{rec_id}.wav"
        return path if path.is_file() else None
