from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

NAMES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MCP_TOKEN")


class SecretStore:
    """API keys and the MCP token. A value saved from the UI wins; otherwise the environment (.env) value is the default.

    Saved values live in a private file (mode 600). They are never sent to the browser: callers only learn where a value comes from.
    With `path=None` nothing touches the disk (tests, --ask).
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._values: dict[str, str] = {}
        if self.path is not None:
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self._values = {k: v for k, v in saved.items() if k in NAMES and isinstance(v, str) and v}
            except (OSError, ValueError):
                pass

    def get(self, name: str) -> str:
        return self._values.get(name) or os.environ.get(name, "")

    def source(self, name: str) -> str:
        """'ui' (saved from the interface), 'env' (default from .env) or 'none'."""
        if self._values.get(name):
            return "ui"
        return "env" if os.environ.get(name) else "none"

    def set(self, name: str, value: str) -> None:
        if name not in NAMES:
            raise ValueError(f"unknown secret {name}")
        self._values[name] = value
        self._save()

    def clear(self, name: str) -> None:
        """Forget the saved value, so the .env default (if any) applies again."""
        self._values.pop(name, None)
        self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # private from the first byte
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(self._values, file)
        os.replace(temporary, self.path)
