from __future__ import annotations

import json
import logging
from pathlib import Path

from ..events import EventBus
from ..secrets import SecretStore
from ..skills import PROVIDERS

logger = logging.getLogger(__name__)

KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}  # .env names: the default when no key was saved from the UI


class AiState:
    """Is AI mode on, which provider answers, and the options editable from the UI (models, cloud transcription).

    A provider is usable once it has an API key: saved from the UI, or the .env default (see `SecretStore`).
    Options saved from the UI win over `config/settings.json`, which stays the default.
    """

    def __init__(self, bus: EventBus, ai_settings: dict, path: str | Path = ".state/ai.json", store: SecretStore | None = None) -> None:
        self.bus = bus
        self.settings = ai_settings
        self.path = Path(path)
        self.store = store or SecretStore(None)
        self.mode = False
        self.provider = str(ai_settings.get("default_provider", "anthropic"))
        self.options: dict = {"models": {}, "stt_model": "", "cloud_stt": None}  # UI overrides; empty / None = use settings.json
        self._load()
        self.reconcile()

    def configured(self) -> list[str]:
        return [name for name in PROVIDERS if self.store.get(KEY_ENV[name])]

    def default_model(self, name: str) -> str:
        return str(self.settings.get("providers", {}).get(name, {}).get("model", ""))

    def model(self, name: str) -> str:
        return self.options["models"].get(name) or self.default_model(name)

    def default_stt_model(self) -> str:
        return str(self.settings.get("providers", {}).get("openai", {}).get("stt_model", "gpt-4o-transcribe"))

    def stt_model(self) -> str:
        return self.options["stt_model"] or self.default_stt_model()

    def cloud_stt(self) -> bool:
        chosen = self.options["cloud_stt"]
        return bool(self.settings.get("cloud_stt", True) if chosen is None else chosen)

    def fingerprint(self, name: str) -> tuple[str, str]:
        """Changes when the key or model of a provider changes, so cached clients are rebuilt."""
        return self.store.get(KEY_ENV[name]), self.model(name)

    def snapshot(self) -> dict:
        return {
            "mode": self.mode, "provider": self.provider,
            "providers": [{"name": n, "configured": n in self.configured(), "model": self.model(n)} for n in PROVIDERS],
        }

    def settings_view(self) -> dict:
        """Everything the settings panel shows. Never contains a key or token: only where each one comes from."""
        return {
            "providers": [
                {"name": n, "key_source": self.store.source(KEY_ENV[n]), "model": self.options["models"].get(n, ""), "default_model": self.default_model(n)}
                for n in PROVIDERS
            ],
            "stt_model": self.options["stt_model"], "default_stt_model": self.default_stt_model(),
            "cloud_stt": self.cloud_stt(),
        }

    def set_options(self, models: dict | None = None, stt_model: str | None = None, cloud_stt: bool | None = None) -> None:
        if models is not None:
            for name in PROVIDERS:
                if name in models:
                    self.options["models"][name] = models[name]
        if stt_model is not None:
            self.options["stt_model"] = stt_model
        if cloud_stt is not None:
            self.options["cloud_stt"] = cloud_stt
        self._changed()

    def reconcile(self) -> None:
        """Keep the mode and provider valid after keys were added or removed."""
        if self.provider not in self.configured() and self.configured():
            self.provider = self.configured()[0]

    def keys_changed(self) -> None:
        self.reconcile()
        self._changed()

    def set_mode(self, on: bool) -> bool:
        """Always allowed: without a key, the chat says so when something is asked."""
        self.mode = on
        self._changed()
        return True

    def set_provider(self, name: str) -> bool:
        if name not in self.configured():
            return False
        self.provider = name
        self._changed()
        return True

    def announce(self) -> None:
        self.bus.publish("ai", **self.snapshot())

    def _changed(self) -> None:
        self.announce()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"mode": self.mode, "provider": self.provider, "options": self.options}), encoding="utf-8")
        except OSError:
            logger.warning("Cannot save the AI mode to %s", self.path)

    def _load(self) -> None:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.mode = bool(saved.get("mode"))
        if saved.get("provider") in PROVIDERS:
            self.provider = saved["provider"]
        options = saved.get("options") or {}
        self.options = {
            "models": {k: v for k, v in (options.get("models") or {}).items() if k in PROVIDERS and isinstance(v, str)},
            "stt_model": options.get("stt_model") if isinstance(options.get("stt_model"), str) else "",
            "cloud_stt": options["cloud_stt"] if isinstance(options.get("cloud_stt"), bool) else None,
        }
