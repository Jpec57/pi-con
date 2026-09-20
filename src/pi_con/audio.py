from __future__ import annotations

import wave
from pathlib import Path

import numpy as np


def read_wav_frames(path: str | Path, sample_rate: int, frame_samples: int) -> list[np.ndarray]:
    """Split a 16 kHz mono 16-bit WAV file into listener-sized frames (last one zero-padded)."""
    with wave.open(str(path), "rb") as wav:
        fmt = (wav.getframerate(), wav.getnchannels(), wav.getsampwidth())
        if fmt != (sample_rate, 1, 2):
            raise ValueError(
                f"{path}: expected {sample_rate} Hz mono 16-bit WAV, got {fmt[0]} Hz, {fmt[1]} channel(s), "
                f"{fmt[2] * 8}-bit. Convert with: ffmpeg -i in.wav -ar {sample_rate} -ac 1 -sample_fmt s16 out.wav"
            )
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
    padding = -len(samples) % frame_samples
    samples = np.concatenate([samples, np.zeros(padding, dtype=np.int16)])
    return [samples[i : i + frame_samples] for i in range(0, len(samples), frame_samples)]
