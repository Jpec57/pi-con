from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Camera:
    name: str  # what the owner says: "Montre la caméra <name>"
    entity: str  # Home Assistant entity id, e.g. camera.rabbit


@dataclass(frozen=True)
class Settings:
    wakeword: str
    threshold: float
    audio_device: str | int | None
    command: tuple[str, ...]
    cooldown_seconds: float = 2.0
    ui_port: int | None = 8080
    wake_phrase: str = ""
    stt_enabled: bool = True
    stt_model_dir: str = "models/stt-fr"
    stt_threads: int = 2
    max_listen_seconds: float = 12.0
    recordings_keep: int = 5  # utterances kept for the "hear it back" panel (0 = none are saved)
    followup_seconds: float = 10.0  # after an answer, keep listening this long for a follow-up without the wake word (0 = off)
    ha_url: str = "http://127.0.0.1:8123"
    ha_token: str = ""
    cameras: tuple[Camera, ...] = ()
    skills_file: str = "config/skills.json"
    mcp_port: int | None = None  # set it to serve MCP on 127.0.0.1:<port> (needs MCP_TOKEN)
    mcp_token: str = ""
    tts: dict | None = None  # the "tts" block of settings.json: spoken replies (Piper)
    ai: dict | None = None  # the "ai" block of settings.json; keys come from the environment
    needle_enabled: bool = True  # fallback for sentences no skill matched (needs cactus-needle)
    skill_threshold: float = 0.8  # 0..1 similarity needed between what was heard and a skill phrase
    sample_rate: int = 16_000
    frame_samples: int = 1_280


def default_phrase(wakeword: str) -> str:
    """'hey_jarvis' -> 'Hey Jarvis'; a custom model path uses its file name."""
    return Path(wakeword).stem.replace("_", " ").replace("-", " ").title()


def load_dotenv(path: str | Path = ".env") -> None:
    """Export KEY=value lines from a .env file without overriding the real environment (systemd sets it on the Pi)."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip() and not key.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())


def load_settings(path: str | Path | None = None) -> Settings:
    config_path = Path(path or os.getenv("PI_CON_CONFIG", "config/settings.json"))
    data = json.loads(config_path.read_text(encoding="utf-8"))
    load_dotenv(config_path.resolve().parent.parent / ".env")
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
        wake_phrase=str(data.get("wake_phrase") or default_phrase(data["wakeword"])),
        stt_enabled=bool(data.get("stt_enabled", True)),
        stt_model_dir=str(data.get("stt_model_dir", "models/stt-fr")),
        stt_threads=int(data.get("stt_threads", 2)),
        max_listen_seconds=float(data.get("max_listen_seconds", 12.0)),
        followup_seconds=float(data.get("followup_seconds", 10.0)),
        recordings_keep=int(data.get("recordings_keep", 5)),
        ha_url=str(data.get("home_assistant_url", "http://127.0.0.1:8123")).rstrip("/"),
        ha_token=os.getenv("HA_TOKEN", ""),
        skills_file=str(data.get("skills_file", "config/skills.json")),
        skill_threshold=float(data.get("skill_threshold", 0.8)),
        needle_enabled=bool(data.get("needle_enabled", True)),
        ai=dict(data.get("ai") or {}),
        tts=dict(data.get("tts") or {}),
        mcp_port=None if data.get("mcp_port") is None else int(data["mcp_port"]),
        mcp_token=os.getenv("MCP_TOKEN", ""),
        cameras=tuple(
            Camera(str(camera["name"]), str(camera["entity"]))
            for camera in data.get("cameras", [])
        ),
    )
