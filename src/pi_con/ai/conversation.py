from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable

from ..events import EventBus
from ..tools import Tool
from .providers import AnthropicProvider, OpenAIProvider, ProviderError
from .state import KEY_ENV, AiState

logger = logging.getLogger(__name__)

SYSTEM = (
    "Tu es Jarvis, l'assistant vocal d'une maison, sur un Raspberry Pi. Réponds en français, en une ou deux phrases courtes "
    "faites pour être dites à voix haute, sans markdown ni liste. Utilise les outils pour afficher une caméra ou une image (show_image) ou agir sur la maison "
    "quand on te le demande; si un outil échoue, dis-le simplement. La parole est transcrite par un système imparfait avec un micro "
    "médiocre: déduis l'intention des mots mal reconnus (par exemple « rabit » ou « rabi » veut dire « rabbit », « chat gpt » veut dire ChatGPT) "
    "et ne fais pas de remarque sur les fautes de transcription."
)


def make_provider(name: str, state: AiState):
    """Build the provider client from the key (UI or .env) and the model (UI or settings.json)."""
    max_tokens = int(state.settings.get("max_output_tokens", 1024))
    if not state.store.get(KEY_ENV[name]):
        raise ProviderError(f"no API key: set {KEY_ENV[name]} in .env or in the settings panel")
    return (AnthropicProvider if name == "anthropic" else OpenAIProvider)(state.store.get(KEY_ENV[name]), state.model(name), max_tokens)


class AiAssistant:
    """Sends what was said to the active provider, lets it use the allowlisted tools, streams the answer to the UI."""

    def __init__(
        self, bus: EventBus, state: AiState, tools: Callable[[], list[Tool]],
        provider_factory: Callable[[str], object] | None = None, history_turns: int = 6, idle_reset: float = 120.0,
    ) -> None:
        self.bus = bus
        self.state = state
        self.tools = tools
        self._factory = provider_factory or (lambda name: make_provider(name, state))
        self._providers: dict[str, tuple[tuple, object]] = {}
        self._history: deque[tuple[str, str]] = deque(maxlen=history_turns)
        self._idle_reset = idle_reset
        self._last = 0.0

    def _provider(self, name: str):
        fingerprint = self.state.fingerprint(name) if name in KEY_ENV else ()
        cached = self._providers.get(name)
        if cached is None or cached[0] != fingerprint:  # the key or model was changed from the UI
            cached = self._providers[name] = (fingerprint, self._factory(name))
        return cached[1]

    def ask(self, text: str, provider: str | None = None) -> str:
        """Answer one sentence. Never raises: problems become a notice in the chat. Returns the reply text."""
        name = provider or self.state.provider
        if time.monotonic() - self._last > self._idle_reset:
            self._history.clear()  # a new conversation: no stale context, and nothing resent that is not needed
        tools = self.tools()
        by_name = {tool.name: tool for tool in tools}

        def execute(tool_name: str, args: dict) -> str:
            tool = by_name.get(tool_name)
            if tool is None:
                return f"unknown tool {tool_name}"
            logger.info("AI tool call: %s %s", tool_name, args)
            try:
                return tool.handler(args or {})
            except Exception as error:  # the model sees the failure and can say so
                return f"error: {error}"

        client = None
        self.bus.publish("thinking", persist=False, active=True)  # the UI shows an animation until the answer (or an error) arrives
        try:
            client = self._provider(name)
            reply = client.run(
                SYSTEM, list(self._history), text, tools, execute,
                lambda chunk: self.bus.publish("ai_delta", persist=False, text=chunk, provider=name),
            )
        except ProviderError as error:
            self.bus.publish("thinking", persist=False, active=False)
            logger.error("AI %s: %s", name, error)
            self.bus.publish("status", state="info", message=f"AI ({name}): {error}")
            return ""
        except Exception:
            self.bus.publish("thinking", persist=False, active=False)
            logger.exception("AI %s crashed", name)
            self.bus.publish("status", state="info", message=f"AI ({name}) failed: see the logs")
            return ""
        self.bus.publish("thinking", persist=False, active=False)
        self._last = time.monotonic()
        self._history.append((text, reply))
        usage = self._usage(client, name)
        if reply:  # a turn that only used a tool has nothing to say (the tool already posted its own notice)
            self.bus.publish("ai_reply", text=reply, provider=name, **({"usage": usage} if usage else {}))
        elif usage:  # it still cost tokens
            self.bus.publish("status", state="info", message=f"AI ({name}): {usage['input']} in / {usage['output']} out tokens")
        return reply

    def _usage(self, client, name: str) -> dict:
        """Tokens in/out of the last sentence, plus its estimated price in euros when `price_per_mtok` (USD per million tokens)
        is set for the provider in settings.json; `usd_to_eur` in the "ai" block is the exchange rate (an approximation)."""
        counted = getattr(client, "last_usage", None)
        if not counted or not (counted.get("input") or counted.get("output")):
            return {}
        usage = {"input": int(counted["input"]), "output": int(counted["output"])}
        price = self.state.settings.get("providers", {}).get(name, {}).get("price_per_mtok")  # {"input": 3, "output": 15}: USD per million tokens
        if isinstance(price, dict) and price.get("input") is not None and price.get("output") is not None:
            dollars = (usage["input"] * float(price["input"]) + usage["output"] * float(price["output"])) / 1e6
            usage["cost_eur"] = round(dollars * float(self.state.settings.get("usd_to_eur", 0.87)), 6)
        return usage
