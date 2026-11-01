import json
import time
import urllib.error
import urllib.request
from types import SimpleNamespace as NS

import numpy as np
import pytest

from pi_con.ai.conversation import AiAssistant
from pi_con.ai.providers import AnthropicProvider, OpenAIProvider, ProviderError
from pi_con.ai.state import AiState
from pi_con.events import EventBus
from pi_con.router import Router
from pi_con.skills import parse_skills
from pi_con.stt import CloudUtterance
from pi_con.tools import Tool
from pi_con.ui import start_ui

SETTINGS = {"default_provider": "anthropic", "providers": {"anthropic": {"model": "m-a"}, "openai": {"model": "m-o"}}}


@pytest.fixture
def keys(monkeypatch):
    def set_keys(*names):
        for env in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(env, raising=False)
        for name in names:
            monkeypatch.setenv({"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[name], "sk-test")
    return set_keys


def events(subscriber):
    out = []
    while not subscriber.empty():
        out.append(subscriber.get_nowait())
    return out


# ---- state -----------------------------------------------------------------------------------------------------
def test_mode_can_be_turned_on_without_a_key(keys, tmp_path):
    keys()
    state = AiState(EventBus(), SETTINGS, tmp_path / "ai.json")
    assert state.configured() == [] and state.set_mode(True) is True and state.mode is True


def test_provider_switch_only_among_configured_and_state_persists(keys, tmp_path):
    keys("openai", "anthropic")
    bus = EventBus()
    live, _ = bus.subscribe()
    state = AiState(bus, SETTINGS, tmp_path / "ai.json")
    assert state.set_provider("openai") and state.set_mode(True)
    assert [e["type"] for e in events(live)] == ["ai", "ai"]
    again = AiState(EventBus(), SETTINGS, tmp_path / "ai.json")  # restart
    assert (again.mode, again.provider) == (True, "openai")
    keys("anthropic")  # the OpenAI key was removed
    assert state.set_provider("openai") is False
    fresh = AiState(EventBus(), SETTINGS, tmp_path / "ai.json")
    assert fresh.provider == "anthropic" and fresh.mode is True
    keys()
    assert AiState(EventBus(), SETTINGS, tmp_path / "ai.json").mode is True  # no key left: still on, the chat reports it


def test_snapshot_lists_both_providers(keys, tmp_path):
    keys("anthropic")
    snap = AiState(EventBus(), SETTINGS, tmp_path / "ai.json").snapshot()
    assert [(p["name"], p["configured"], p["model"]) for p in snap["providers"]] == [("openai", False, "m-o"), ("anthropic", True, "m-a")]


# ---- assistant ---------------------------------------------------------------------------------------------------
class FakeProvider:
    def __init__(self, reply="Bonjour.", error=None):
        self.reply, self.error, self.calls = reply, error, []

    def run(self, system, past, text, tools, execute, on_text):
        self.calls.append((list(past), text, [t.name for t in tools]))
        if self.error:
            raise self.error
        for word in self.reply.split(" "):
            on_text(word + " ")
        return self.reply


def assistant(keys, tmp_path, provider, tools=()):
    keys("anthropic")
    bus = EventBus()
    live, _ = bus.subscribe()
    state = AiState(bus, SETTINGS, tmp_path / "ai.json")
    return AiAssistant(bus, state, lambda: list(tools), provider_factory=lambda name: provider), live, state


def test_reply_is_streamed_then_published_and_remembered(keys, tmp_path):
    provider = FakeProvider("Il fait beau.")
    ai, live, _ = assistant(keys, tmp_path, provider)
    assert ai.ask("quel temps") == "Il fait beau."
    kinds = [e["type"] for e in events(live)]
    assert kinds[:-1] == ["ai_delta"] * 3 and kinds[-1] == "ai_reply"
    ai.ask("et demain")
    assert provider.calls[1][0] == [("quel temps", "Il fait beau.")]  # history resent


def test_history_is_dropped_after_idle_and_bounded(keys, tmp_path):
    provider = FakeProvider()
    ai, _, _ = assistant(keys, tmp_path, provider)
    ai._idle_reset = 0.0
    ai.ask("un")
    time.sleep(0.01)
    ai.ask("deux")
    assert provider.calls[1][0] == []


def test_provider_errors_become_a_notice_not_a_crash(keys, tmp_path):
    ai, live, _ = assistant(keys, tmp_path, FakeProvider(error=ProviderError("Anthropic refused the API key")))
    assert ai.ask("salut") == ""
    assert any("refused the API key" in e.get("message", "") for e in events(live))
    ai2, live2, _ = assistant(keys, tmp_path, FakeProvider(error=RuntimeError("boom")))
    assert ai2.ask("salut") == "" and any("failed" in e.get("message", "") for e in events(live2))


def test_a_tool_only_turn_posts_no_empty_bubble(keys, tmp_path):
    ai, live, _ = assistant(keys, tmp_path, FakeProvider(reply=""))
    ai.ask("montre rabbit")
    assert "ai_reply" not in [e["type"] for e in events(live)]


# ---- providers against fake SDK clients ---------------------------------------------------------------------------
TOOL = Tool("show_camera", "Show a camera.", {"type": "object", "properties": {"camera": {"type": "string"}}}, lambda a: "shown")


class FakeAnthropicStream:
    def __init__(self, texts, final):
        self.text_stream, self._final = iter(texts), final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._final


def test_anthropic_tool_round_trip():
    steps = [
        FakeAnthropicStream(["Je mets"], NS(stop_reason="tool_use", content=[NS(type="text", text="Je mets"), NS(type="tool_use", id="t1", name="show_camera", input={"camera": "rabbit"})])),
        FakeAnthropicStream(["C'est fait."], NS(stop_reason="end_turn", content=[NS(type="text", text="C'est fait.")])),
    ]
    sent = []
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider._anthropic = __import__("anthropic")
    provider.model, provider.max_tokens = "m", 100
    provider.client = NS(messages=NS(stream=lambda **kw: sent.append(kw) or steps[len(sent) - 1]))
    spoken, executed = [], []
    reply = provider.run("sys", [("avant", "ok")], "montre rabbit", [TOOL], lambda n, a: executed.append((n, a)) or "shown", spoken.append)
    assert reply == "Je mets C'est fait." and executed == [("show_camera", {"camera": "rabbit"})] and spoken == ["Je mets", "C'est fait."]
    assert sent[0]["messages"][:2] == [{"role": "user", "content": "avant"}, {"role": "assistant", "content": "ok"}]
    assert sent[0]["tools"][0]["input_schema"] == TOOL.schema and sent[0]["output_config"] == {"effort": "low"}
    assert sent[1]["messages"][-1]["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": "shown"}


def test_anthropic_errors_are_mapped():
    anthropic = __import__("anthropic")
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider._anthropic, provider.model, provider.max_tokens = anthropic, "m", 10

    def boom(**_kw):
        raise anthropic.AuthenticationError("no", response=NS(status_code=401, headers={}, request=None), body=None)
    provider.client = NS(messages=NS(stream=boom))
    with pytest.raises(ProviderError, match="API key"):
        provider.run("s", [], "x", [], lambda n, a: "", lambda t: None)


def chunk(content=None, calls=None):
    return NS(choices=[NS(delta=NS(content=content, tool_calls=calls))])


def test_openai_streamed_tool_round_trip():
    first = [chunk(calls=[NS(index=0, id="c1", function=NS(name="show_camera", arguments='{"camera":'))]),
             chunk(calls=[NS(index=0, id=None, function=NS(name=None, arguments='"rabbit"}'))])]
    second = [chunk(content="C'est "), chunk(content="fait."), NS(choices=[])]
    sent = []
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider._openai, provider.model, provider.max_tokens = __import__("openai"), "m", 100
    provider.client = NS(chat=NS(completions=NS(create=lambda **kw: sent.append(kw) or iter([first, second][len(sent) - 1]))))
    executed, spoken = [], []
    reply = provider.run("sys", [], "montre rabbit", [TOOL], lambda n, a: executed.append((n, a)) or "shown", spoken.append)
    assert reply == "C'est fait." and executed == [("show_camera", {"camera": "rabbit"})] and spoken == ["C'est ", "fait."]
    assert sent[0]["messages"][0] == {"role": "system", "content": "sys"} and sent[0]["tools"][0]["function"]["name"] == "show_camera"
    assert sent[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "shown"}


# ---- routing -----------------------------------------------------------------------------------------------------
CONTROL = {"skills": [
    {"name": "Mode IA", "phrases": ["mode ia"], "action": {"type": "ai_mode", "mode": "on"}},
    {"name": "Mode normal", "phrases": ["mode normal"], "action": {"type": "ai_mode", "mode": "off"}},
    {"name": "Claude", "phrases": ["utilise claude"], "action": {"type": "ai_provider", "provider": "anthropic"}},
    {"name": "ChatGPT", "phrases": ["utilise chatgpt"], "action": {"type": "ai_provider", "provider": "openai"}},
    {"name": "Voir rabbit", "phrases": ["vidéo rabbit"], "action": {"type": "camera", "camera": "rabbit"}},
]}


def router(keys, tmp_path, *names):
    keys(*names)
    path = tmp_path / "skills.json"
    path.write_text(json.dumps(CONTROL), encoding="utf-8")
    bus = EventBus()
    live, _ = bus.subscribe()
    state = AiState(bus, SETTINGS, tmp_path / "ai.json")
    provider = FakeProvider("Réponse.")
    ai = AiAssistant(bus, state, lambda: [], provider_factory=lambda name: provider)
    return Router(bus, path, {"rabbit": "camera.rabbit"}, None, 0.8, None, ai), state, provider, live


def test_ai_mode_sends_everything_but_control_phrases_to_the_llm(keys, tmp_path):
    r, state, provider, _ = router(keys, tmp_path, "anthropic")
    assert r.handle("vidéo rabbit")  # off: the skill runs, the LLM is not asked
    assert provider.calls == []
    assert r.handle("mode ia") and state.mode is True
    assert r.handle("vidéo rabbit")  # on: even a skill phrase goes to the LLM
    assert [c[1] for c in provider.calls] == ["vidéo rabbit"]
    assert r.handle("quelle heure") and len(provider.calls) == 2
    assert r.handle("mode normal") and state.mode is False and len(provider.calls) == 2  # control phrase handled locally
    assert r.handle("quelle heure") is False


def test_voice_switch_between_providers_and_missing_keys(keys, tmp_path):
    r, state, _, live = router(keys, tmp_path, "anthropic", "openai")
    r.handle("utilise chatgpt")
    assert state.provider == "openai"
    r.handle("utilise claude")
    assert state.provider == "anthropic"
    r2, state2, _, live2 = router(keys, tmp_path, "anthropic")
    events(live2)
    r2.handle("utilise chatgpt")
    assert state2.provider == "anthropic" and any("pas configuré" in e.get("message", "") for e in events(live2))
    r3, state3, _, live3 = router(keys, tmp_path)
    r3.handle("mode ia")
    assert state3.mode is True and any("Mode IA activé" in e.get("message", "") for e in events(live3))


def test_control_skills_are_validated():
    with pytest.raises(ValueError, match="on, off or toggle"):
        parse_skills({"skills": [{"name": "x", "phrases": ["a"], "action": {"type": "ai_mode", "mode": "maybe"}}]})
    with pytest.raises(ValueError, match="provider"):
        parse_skills({"skills": [{"name": "x", "phrases": ["a"], "action": {"type": "ai_provider", "provider": "bard"}}]})


# ---- cloud transcription ---------------------------------------------------------------------------------------
class LocalUtterance:
    def __init__(self, finish_at=2):
        self.n, self.finish_at = 0, finish_at

    def accept(self, frame):
        self.n += 1
        return "montre rabit", self.n >= self.finish_at


FRAME = np.zeros(1280, dtype=np.int16)


def drive(utterance, steps=200):
    for _ in range(steps):
        text, finished = utterance.accept(FRAME)
        if finished:
            return text
        time.sleep(0.005)
    raise AssertionError("never finished")


def test_cloud_text_replaces_local_text_after_the_audio_is_complete():
    heard = []
    u = CloudUtterance(LocalUtterance(), lambda pcm: heard.append(len(pcm)) or "montre rabbit")
    assert drive(u) == "montre rabbit" and heard[0] >= 2 * 1280


def test_cloud_failure_or_empty_text_keeps_the_local_text():
    assert drive(CloudUtterance(LocalUtterance(), lambda pcm: "")) == "montre rabit"

    def explode(pcm):
        raise RuntimeError("network")
    assert drive(CloudUtterance(LocalUtterance(), explode)) == "montre rabit"


def test_slow_cloud_is_abandoned_after_the_timeout():
    u = CloudUtterance(LocalUtterance(), lambda pcm: time.sleep(1) or "tard", timeout=0.05)
    assert drive(u) == "montre rabit"


# ---- UI endpoints ------------------------------------------------------------------------------------------------
def call(url, method="GET"):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method=method, data=b"" if method == "POST" else None), timeout=5) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_ai_endpoints(keys, tmp_path):
    keys()
    bus = EventBus()
    state = AiState(bus, SETTINGS, tmp_path / "ai.json")
    server = start_ui(bus, 0, host="127.0.0.1", ai_state=state)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert json.loads(call(base + "/ai")[1])["mode"] is False
        status, body = call(base + "/ai/mode?on=1", "POST")
        assert status == 200 and state.mode is True  # allowed without a key
        keys("anthropic", "openai")
        assert call(base + "/ai/provider?name=openai", "POST")[0] == 200 and state.provider == "openai"
        assert call(base + "/ai/provider?name=bard", "POST")[0] == 409
        assert call(base + "/ai/nope", "POST")[0] == 404
    finally:
        server.shutdown()
        server.server_close()
