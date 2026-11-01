from __future__ import annotations

import json
import logging
from collections.abc import Callable

from ..tools import Tool

logger = logging.getLogger(__name__)

MAX_STEPS = 4  # model <-> tool round trips for one spoken sentence


class ProviderError(Exception):
    """A failure worth showing the owner in the chat (bad key, no credit, wrong model name, network)."""


Execute = Callable[[str, dict], str]


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, max_tokens: int = 1024) -> None:
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model, self.max_tokens = model, max_tokens

    def run(self, system: str, past: list[tuple[str, str]], text: str, tools: list[Tool], execute: Execute, on_text: Callable[[str], None]) -> str:
        a = self._anthropic
        self.last_usage = {"input": 0, "output": 0}  # tokens billed for this sentence, all tool round trips included
        messages: list[dict] = [m for user, reply in past for m in ({"role": "user", "content": user}, {"role": "assistant", "content": reply})]
        messages.append({"role": "user", "content": text})
        specs = [{"name": t.name, "description": t.description, "input_schema": t.schema} for t in tools]
        spoken: list[str] = []
        try:
            for _ in range(MAX_STEPS):
                with self.client.messages.stream(
                    model=self.model, max_tokens=self.max_tokens, system=system, messages=messages, tools=specs or a.NOT_GIVEN,
                    output_config={"effort": "low"},  # a spoken answer: speed over deliberation
                ) as stream:
                    for chunk in stream.text_stream:
                        on_text(chunk)
                    final = stream.get_final_message()
                usage = getattr(final, "usage", None)
                if usage is not None:  # cached prompt tokens are input too (billed at another rate, but they were sent)
                    self.last_usage["input"] += sum(int(getattr(usage, f, 0) or 0) for f in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                    self.last_usage["output"] += int(getattr(usage, "output_tokens", 0) or 0)
                spoken += [block.text for block in final.content if block.type == "text"]
                if final.stop_reason == "refusal":
                    return "Je ne peux pas répondre à ça."
                uses = [block for block in final.content if block.type == "tool_use"]
                if final.stop_reason != "tool_use" or not uses:
                    break
                messages.append({"role": "assistant", "content": final.content})
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": use.id, "content": execute(use.name, use.input)} for use in uses
                ]})
        except a.AuthenticationError as error:
            raise ProviderError("Anthropic refused the API key (ANTHROPIC_API_KEY)") from error
        except a.NotFoundError as error:
            raise ProviderError(f"Anthropic model '{self.model}' not found: change it in config/settings.json") from error
        except a.RateLimitError as error:
            raise ProviderError("Anthropic rate limit or no credit left") from error
        except a.APIStatusError as error:
            raise ProviderError(f"Anthropic error {error.status_code}: {error.message}") from error
        except a.APIConnectionError as error:
            raise ProviderError("Cannot reach Anthropic (network)") from error
        return " ".join(spoken).strip()


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str, max_tokens: int = 1024) -> None:
        import openai

        self._openai = openai
        self.client = openai.OpenAI(api_key=api_key)
        self.model, self.max_tokens = model, max_tokens

    def run(self, system: str, past: list[tuple[str, str]], text: str, tools: list[Tool], execute: Execute, on_text: Callable[[str], None]) -> str:
        o = self._openai
        self.last_usage = {"input": 0, "output": 0}
        messages: list[dict] = [{"role": "system", "content": system}]
        messages += [m for user, reply in past for m in ({"role": "user", "content": user}, {"role": "assistant", "content": reply})]
        messages.append({"role": "user", "content": text})
        specs = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.schema}} for t in tools]
        spoken: list[str] = []
        try:
            for _ in range(MAX_STEPS):
                stream = self.client.chat.completions.create(
                    model=self.model, messages=messages, stream=True, max_completion_tokens=self.max_tokens,
                    stream_options={"include_usage": True},  # the last chunk carries the token counts
                    reasoning_effort="none",  # a spoken answer: speed over deliberation (and tools need it off on this model)
                    **({"tools": specs} if specs else {}),
                )
                content: list[str] = []
                calls: dict[int, dict] = {}
                for chunk in stream:
                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        self.last_usage["input"] += int(getattr(usage, "prompt_tokens", 0) or 0)
                        self.last_usage["output"] += int(getattr(usage, "completion_tokens", 0) or 0)
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.content:
                        content.append(delta.content)
                        on_text(delta.content)
                    for call in delta.tool_calls or []:
                        entry = calls.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
                        entry["id"] = call.id or entry["id"]
                        if call.function:
                            entry["name"] += call.function.name or ""
                            entry["arguments"] += call.function.arguments or ""
                spoken += content
                if not calls:
                    break
                messages.append({"role": "assistant", "content": "".join(content) or None, "tool_calls": [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}} for c in calls.values()
                ]})
                for c in calls.values():
                    try:
                        args = json.loads(c["arguments"] or "{}")
                    except ValueError:
                        args = {}
                    messages.append({"role": "tool", "tool_call_id": c["id"], "content": execute(c["name"], args)})
        except o.AuthenticationError as error:
            raise ProviderError("OpenAI refused the API key (OPENAI_API_KEY)") from error
        except o.NotFoundError as error:
            raise ProviderError(f"OpenAI model '{self.model}' not found: change it in config/settings.json") from error
        except o.RateLimitError as error:
            raise ProviderError("OpenAI rate limit or no credit left") from error
        except o.APIStatusError as error:
            raise ProviderError(f"OpenAI error {error.status_code}: {error.message}") from error
        except o.APIConnectionError as error:
            raise ProviderError("Cannot reach OpenAI (network)") from error
        return "".join(spoken).strip()
