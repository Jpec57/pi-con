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
    cooldown_seconds: float = 2.0
    ui_port: int | None = 8080
    stt_enabled: bool = True
    stt_model_dir: str = "models/stt-fr"
    stt_threads: int = 2
    max_listen_seconds: float = 12.0
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
        cooldown_seconds=float(data.get("cooldown_seconds", 2.0)),
        ui_port=None if data.get("ui_port", 8080) is None else int(data.get("ui_port", 8080)),
        stt_enabled=bool(data.get("stt_enabled", True)),
        stt_model_dir=str(data.get("stt_model_dir", "models/stt-fr")),
        stt_threads=int(data.get("stt_threads", 2)),
        max_listen_seconds=float(data.get("max_listen_seconds", 12.0)),
    )
