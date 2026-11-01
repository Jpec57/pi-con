import json
import threading

import numpy as np

from pi_con.events import EventBus
from pi_con.router import Router
from pi_con.skills import parse_skills
from pi_con.tts import Speaker, clean_for_speech
from pi_con import listener
from pi_con.config import Settings
import pytest


class Fakes:
    def __init__(self):
        self.synthesized, self.played, self.stopped = [], [], 0

    def synthesize(self, text):
        self.synthesized.append(text)
        return np.ones(100, dtype=np.int16), 22_050

    def play(self, audio, rate, device):
        self.played.append((len(audio), rate, device))

    def stop(self):
        self.stopped += 1


def make(tmp_path, enabled=True, bus=None, **kwargs):
    fakes = Fakes()
    speaker = Speaker(bus or EventBus(), fakes.synthesize, fakes.play, fakes.stop, enabled=enabled, path=tmp_path / "tts.json", **kwargs)
    return speaker, fakes


def test_clean_for_speech_strips_markdown_and_links():
    text = "**Salut** ! Voir [le site](https://x.fr) ou https://y.fr\n- un point\n```code```"
    assert clean_for_speech(text) == "Salut ! Voir le site ou un point"


def test_long_answers_are_cut_at_a_sentence_end():
    spoken = clean_for_speech("Une phrase. " * 100)
    assert len(spoken) <= 600 and spoken.endswith(".")


def test_nothing_is_spoken_while_the_option_is_off(tmp_path):
    speaker, fakes = make(tmp_path, enabled=False)
    assert speaker.say("Bonjour") is False
    assert not speaker._speak_next(timeout=0)
    assert fakes.played == []


def test_speaks_on_the_output_device_and_is_busy_around_it(tmp_path):
    speaker, fakes = make(tmp_path, output_device="usb")
    states = []
    fakes.play = lambda *a: states.append(speaker.busy())  # busy while the sound plays
    speaker.play = fakes.play
    speaker.say("Bonjour")
    assert speaker.busy()  # muted from the moment it is queued: no gap for the microphone to hear the start
    assert speaker._speak_next(timeout=0)
    assert states == [True]
    assert speaker.busy()  # short tail after the sound: the room still echoes
    assert fakes.synthesized == ["Bonjour"]


def test_a_failing_voice_never_raises_and_posts_a_notice(tmp_path):
    bus = EventBus()
    speaker, fakes = make(tmp_path, bus=bus)

    def boom(_text):
        raise ImportError("no piper")

    speaker.synthesize = boom
    speaker.say("Bonjour")
    assert speaker._speak_next(timeout=0)
    assert any(e["type"] == "status" and "piper" in e["message"] for e in bus.subscribe()[1])
    assert not speaker._speaking


def test_stop_drops_queued_sentences(tmp_path):
    speaker, fakes = make(tmp_path)
    speaker.say("un")
    speaker.say("deux")
    speaker.stop()
    assert not speaker._speak_next(timeout=0)
    assert fakes.played == []


def test_toggle_is_saved_and_wins_over_the_default(tmp_path):
    speaker, _ = make(tmp_path, enabled=False)
    speaker.set_enabled(True)
    assert json.loads((tmp_path / "tts.json").read_text()) == {"enabled": True}
    again, _ = make(tmp_path, enabled=False)
    assert again.enabled is True


def test_ai_answers_and_skill_sentences_are_read_from_the_bus(tmp_path):
    bus = EventBus()
    speaker, fakes = make(tmp_path, bus=bus)
    done = threading.Event()
    speaker.play = lambda *a: (fakes.played.append(a), done.set())
    speaker.start()
    bus.publish("ai_delta", persist=False, text="pas ça")  # streaming chunks are not read
    bus.publish("ai_reply", text="Il fait beau.", provider="anthropic")
    assert done.wait(5)
    assert fakes.synthesized == ["Il fait beau."]


def test_skill_say_and_voice_control(tmp_path):
    bus = EventBus()
    speaker, _ = make(tmp_path, enabled=False, bus=bus)
    skills = parse_skills({"skills": [
        {"name": "fermer", "phrases": ["ferme la vidéo"], "action": {"type": "camera_hide"}, "say": "C'est fermé."},
        {"name": "voix", "phrases": ["active la voix"], "action": {"type": "tts_mode", "mode": "on"}},
    ]})
    path = tmp_path / "skills.json"
    path.write_text(json.dumps({"skills": [{"name": s.name, "phrases": list(s.phrases), "action": s.action, "say": s.say} for s in skills]}))
    router = Router(bus, path, {}, None, 0.8, speaker=speaker)
    subscriber, _ = bus.subscribe(after_id=1 << 62)
    assert router.handle("ferme la vidéo")
    assert "say" in {subscriber.get(timeout=1)["type"] for _ in range(3)}  # status, camera, say
    assert router.handle("active la voix")
    assert speaker.enabled
    assert speaker._queue.get_nowait()[1] == "Voix activée"


def test_tts_mode_needs_on_or_off():
    with pytest.raises(ValueError):
        parse_skills({"skills": [{"name": "x", "phrases": ["a b"], "action": {"type": "tts_mode", "mode": "maybe"}}]})


def test_listener_ignores_the_microphone_while_the_assistant_speaks(monkeypatch):
    seen = []

    class Model:
        def predict(self, _frame):
            seen.append(1)
            raise KeyboardInterrupt

        def reset(self):
            pass

    class Stream:
        def __init__(self, **_kw): pass
        def start(self): pass
        def abort(self): pass
        def close(self): pass

    monkeypatch.setattr(listener, "load_model", lambda name: Model())
    monkeypatch.setattr(listener.sd, "InputStream", Stream)
    speaking = [True, True, False]
    wl = listener.WakeWordListener(Settings("hey_jarvis", 0.5, None, ("true",)), lambda: None, muted=lambda: speaking.pop(0))
    for _ in range(3):
        wl.frames.put(np.zeros(1280, dtype=np.int16))
    with pytest.raises(KeyboardInterrupt):
        wl.run_forever()
    assert len(seen) == 1  # the two muted frames never reached the wake word model


def test_ui_toggle_and_cross_site_protection(tmp_path):
    import urllib.error
    import urllib.request

    from pi_con.ui import start_ui

    bus = EventBus()
    speaker, fakes = make(tmp_path, enabled=False, bus=bus)
    server = start_ui(bus, 0, host="127.0.0.1", speaker=speaker)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        def call(path, method="POST", origin=None):
            request = urllib.request.Request(base + path, method=method, data=b"" if method == "POST" else None)
            if origin:
                request.add_header("Origin", origin)
            try:
                with urllib.request.urlopen(request, timeout=5) as reply:
                    return reply.status, reply.read()
            except urllib.error.HTTPError as error:
                return error.code, b""

        assert json.loads(call("/tts", "GET")[1]) == {"enabled": False, "voice": "fr_FR-siwis-medium"}
        assert call("/tts/mode?on=1", origin="https://evil.example")[0] == 403 and not speaker.enabled
        assert call("/tts/mode?on=1")[0] == 200 and speaker.enabled
        assert call("/tts/test")[0] == 202 and speaker._queue.qsize() == 1
        assert call("/tts/stop")[0] == 202 and speaker._queue.qsize() == 0
    finally:
        server.shutdown()
        server.server_close()


# ---- canned replies ---------------------------------------------------------------------------------------------
def test_canned_reply_picks_a_variant_and_fills_the_time():
    from datetime import datetime

    from pi_con.router import canned_reply

    assert canned_reply({"text": "Bonjour"}) == "Bonjour"
    assert canned_reply({"text": ["a", "b"]}) in ("a", "b")
    now = datetime(2026, 11, 1, 16, 5)  # a Sunday
    assert canned_reply({"text": "Il est {heure}, {date}."}, now) == "Il est 16 heures 05, dimanche 1 novembre."
    assert canned_reply({"text": "{heure}"}, datetime(2026, 11, 1, 12, 0)) == "midi"


def test_reply_skill_answers_locally_even_in_ai_mode(tmp_path):
    bus = EventBus()
    path = tmp_path / "skills.json"
    path.write_text(json.dumps({"skills": [{"name": "cv", "phrases": ["comment ça va"], "action": {"type": "reply", "text": ["Très bien, et toi ?"]}}]}))

    class Ai:  # AI mode on: this stand-in fails the test if the sentence is sent to the LLM
        state = type("S", (), {"mode": True})()

        def ask(self, _text):
            raise AssertionError("sent to the LLM")

    router = Router(bus, path, {}, None, 0.8, ai=Ai())
    subscriber, _ = bus.subscribe(after_id=1 << 62)
    assert router.handle("hey comment ça va")
    events = [subscriber.get(timeout=1) for _ in range(2)]  # status notice, then the answer
    reply = next(e for e in events if e["type"] == "ai_reply")
    assert (reply["text"], reply["provider"]) == ("Très bien, et toi ?", "local")


def test_reply_needs_text():
    for bad in ({"type": "reply"}, {"type": "reply", "text": []}, {"type": "reply", "text": [1]}):
        with pytest.raises(ValueError):
            parse_skills({"skills": [{"name": "x", "phrases": ["a b"], "action": bad}]})


def test_shipped_skills_file_is_valid_and_greetings_do_not_collide():
    from pi_con.skills import best_match, load_skills

    skills = load_skills("config/skills.json")
    for heard, expected in (("comment ça va", "Comment ça va"), ("moi ça va bien", "Ça va bien"), ("salut", "Bonjour"), ("quelle heure est-il", "Quelle heure")):
        assert best_match(heard, skills, 0.8).skill.name == expected
    assert best_match("il fait beau", skills, 0.8) is None


# ---- follow-up window ---------------------------------------------------------------------------------------------
def test_follow_up_opens_after_an_answer_was_read_and_not_after_a_plain_sentence(tmp_path):
    speaker, fakes = make(tmp_path)
    opened = []
    speaker.after_reply = lambda: opened.append(len(fakes.played))
    speaker.say("Une phrase de skill")  # not an AI answer
    speaker._speak_next(timeout=0)
    assert opened == []
    speaker.say("Une réponse", followup=True)
    speaker._speak_next(timeout=0)
    assert opened == [2]  # opened after the second sentence finished playing, not before


def test_follow_up_opens_at_once_when_the_answer_is_not_read_aloud(tmp_path):
    bus = EventBus()
    speaker, _ = make(tmp_path, enabled=False, bus=bus)
    opened = threading.Event()
    speaker.after_reply = opened.set
    speaker.start()
    bus.publish("ai_reply", text="Réponse affichée seulement", provider="openai")
    assert opened.wait(5)


def test_stopping_cancels_the_follow_up(tmp_path):
    speaker, _ = make(tmp_path)
    opened = []
    speaker.after_reply = lambda: opened.append(1)
    speaker.say("Une réponse", followup=True)
    speaker.stop()
    speaker._speak_next(timeout=0)
    assert opened == [] and not speaker.busy() or opened == []


class FollowUpModel:
    def predict(self, _frame):
        return {"hey_jarvis": np.float32(0.0)}

    def reset(self):
        pass


class SilentUtterance:
    """Like sherpa: reports an end of speech after a short silence, with nothing heard."""

    def __init__(self, texts):
        self.texts, self.frames = texts, 0

    def accept(self, _frame):
        self.frames += 1
        text = self.texts.get(self.frames, "")
        return text, self.frames >= 3 and not text or self.frames >= 8


def follow_up_listener(monkeypatch, clock, texts):
    class Stream:
        def __init__(self, **_kw): pass
        def start(self): pass
        def abort(self): pass
        def close(self): pass

    monkeypatch.setattr(listener, "load_model", lambda name: FollowUpModel())
    monkeypatch.setattr(listener.sd, "InputStream", Stream)
    monkeypatch.setattr(listener.time, "monotonic", lambda: clock[0])
    utterance = SilentUtterance(texts)
    bus = EventBus()
    transcriber = type("T", (), {"start": lambda self: utterance})()
    wl = listener.WakeWordListener(Settings("hey_jarvis", 0.5, None, ("true",), max_listen_seconds=12), lambda: None, bus=bus, transcriber=transcriber)
    return wl, bus, utterance


def feed(wl, clock, seconds, step=0.08):
    """Hand the loop silent frames, advancing the fake clock, until `seconds` have passed; then stop it."""
    frame = np.zeros(1280, dtype=np.int16)
    end = clock[0] + seconds

    def get(timeout=None):
        if clock[0] >= end:
            raise KeyboardInterrupt
        clock[0] += step
        return frame

    wl.frames.get = get
    with pytest.raises(KeyboardInterrupt):
        wl.run_forever()


def test_follow_up_waits_ten_seconds_for_a_first_word_despite_the_recogniser_end_of_speech(monkeypatch):
    clock = [100.0]
    wl, bus, utterance = follow_up_listener(monkeypatch, clock, {})
    assert wl.request_followup(10)
    feed(wl, clock, 6)  # past the recogniser's own "nothing said" end: still waiting
    assert utterance.frames > 50 and not [e for e in bus.subscribe()[1] if e["type"] == "transcript"]
    assert wl._followup_wait == 0.0


def test_follow_up_gives_up_after_the_wait_and_then_goes_back_to_the_wake_word(monkeypatch):
    clock = [100.0]
    wl, bus, utterance = follow_up_listener(monkeypatch, clock, {})
    captures = []
    sub, _ = bus.subscribe()
    wl.request_followup(2)
    feed(wl, clock, 4)
    while not sub.empty():
        event = sub.get()
        if event["type"] == "capture":
            captures.append(event["active"])
    assert captures == [True, False]


def test_follow_up_sentence_is_transcribed(monkeypatch):
    clock = [100.0]
    wl, bus, utterance = follow_up_listener(monkeypatch, clock, {20: "et demain ?"})
    wl.request_followup(10)
    feed(wl, clock, 3)
    assert [e["text"] for e in bus.subscribe()[1] if e["type"] == "transcript"] == ["et demain ?"]


def test_follow_up_is_refused_without_speech_to_text_or_when_off(monkeypatch):
    clock = [0.0]
    wl, _, _ = follow_up_listener(monkeypatch, clock, {})
    assert wl.request_followup(0) is False
    wl.transcriber = None
    assert wl.request_followup(10) is False


# ---- token usage ---------------------------------------------------------------------------------------------------
def test_reply_carries_the_tokens_and_the_price_when_known():
    from pi_con.ai.conversation import AiAssistant
    from pi_con.ai.state import AiState

    class Client:
        last_usage = {"input": 1200, "output": 80}

        def run(self, *_a):
            return "Il fait beau."

    settings = {"providers": {"anthropic": {"price_per_mtok": {"input": 3, "output": 15}}, "openai": {}}}
    bus = EventBus()
    ai = AiAssistant(bus, AiState(bus, settings, path="/dev/null"), lambda: [], provider_factory=lambda name: Client())
    ai.ask("quel temps ?", "anthropic")
    ai.ask("quel temps ?", "openai")
    replies = [e for e in bus.subscribe()[1] if e["type"] == "ai_reply"]
    assert replies[0]["usage"] == {"input": 1200, "output": 80, "cost": 0.0048}
    assert replies[1]["usage"] == {"input": 1200, "output": 80}  # no price configured: tokens only


def test_tokens_are_reported_even_when_the_turn_only_used_a_tool():
    from pi_con.ai.conversation import AiAssistant
    from pi_con.ai.state import AiState

    class Client:
        last_usage = {"input": 300, "output": 20}

        def run(self, *_a):
            return ""

    bus = EventBus()
    AiAssistant(bus, AiState(bus, {}, path="/dev/null"), lambda: [], provider_factory=lambda name: Client()).ask("montre rabbit", "openai")
    assert any("300 in / 20 out" in e.get("message", "") for e in bus.subscribe()[1])


def test_providers_add_up_the_usage_of_every_round_trip():
    from types import SimpleNamespace as NS

    from pi_con.ai.providers import OpenAIProvider

    def chunk(content=None, usage=None, calls=None):
        choices = [NS(delta=NS(content=content, tool_calls=calls))] if (content or calls) else []
        return NS(choices=choices, usage=usage)

    call = NS(index=0, id="c1", function=NS(name="t", arguments="{}"))
    streams = iter([
        [chunk(calls=[call]), chunk(usage=NS(prompt_tokens=100, completion_tokens=10))],
        [chunk("Voilà."), chunk(usage=NS(prompt_tokens=150, completion_tokens=5))],
    ])
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider._openai = __import__("openai")
    provider.model, provider.max_tokens = "m", 100
    seen = {}
    provider.client = NS(chat=NS(completions=NS(create=lambda **kw: seen.update(kw) or next(streams))))
    tool = type("Tool", (), {"name": "t", "description": "", "schema": {}})()
    assert provider.run("s", [], "x", [tool], lambda n, a: "ok", lambda c: None) == "Voilà."
    assert provider.last_usage == {"input": 250, "output": 15}
    assert seen["stream_options"] == {"include_usage": True}
