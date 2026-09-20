from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    wakeword: str
    threshold: float
    audio_device: str | int | None
    command: tuple[str, ...]
    sample_rate: int = 16_000
    frame_samples: int = 1_280


def load_settings(path: str | Path | None = None) -> Settings:
    config_path = Path(path or os.getenv("PI_CON_CONFIG", "config/settings.json"))
    data = json.loads(config_path.read_text(encoding="utf-8"))
    command = tuple(data.get("command", []))
    if not command:
        raise ValueError("config command must contain at least one program")
    return Settings(
        wakeword=data["wakeword"],
        threshold=float(data.get("threshold", 0.5)),
        audio_device=data.get("audio_device"),
        command=command,
    )
