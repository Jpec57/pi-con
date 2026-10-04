from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any


class RingLogHandler(logging.Handler):
    """Keeps the last log lines in memory so the UI can show them."""

    def __init__(self, size: int = 200) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=size)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


class DebugState:
    """Counters the audio loop updates and the UI reads (plain ints: races are harmless here)."""

    def __init__(self) -> None:
        self.started = time.time()
        self.frames = 0
        self.last_frame = time.monotonic()  # monotonic time of the last audio block from the mic
        self.overflows = 0
        self.stalls = 0
        self.stream_restarts = 0
        self.capture_rate: int | None = None
        self.device = "?"
        self.stream = "starting"
        self.last_error = ""
        self._lock = threading.Lock()

    def snapshot(self) -> dict[str, Any]:
        return {
            "uptime_s": round(time.time() - self.started),
            "audio": {
                "stream": self.stream,
                "device": self.device,
                "capture_rate": self.capture_rate,
                "blocks": self.frames,
                "last_block_age_s": round(time.monotonic() - self.last_frame, 1),
                "overflows": self.overflows,
                "stalls": self.stalls,
                "stream_restarts": self.stream_restarts,
                "last_error": self.last_error,
            },
            "system": system_info(),
        }


def system_info() -> dict[str, Any]:
    info: dict[str, Any] = {"load": [round(x, 2) for x in os.getloadavg()]}
    try:
        info["cpu_temp_c"] = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        pass
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                info["memory_mb"] = int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return info
