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

    settings = {"usd_to_eur": 0.5, "providers": {"anthropic": {"price_per_mtok": {"input": 3, "output": 15}}, "openai": {}}}
    bus = EventBus()
    ai = AiAssistant(bus, AiState(bus, settings, path="/dev/null"), lambda: [], provider_factory=lambda name: Client())
    ai.ask("quel temps ?", "anthropic")
    ai.ask("quel temps ?", "openai")
    replies = [e for e in bus.subscribe()[1] if e["type"] == "ai_reply"]
    assert replies[0]["usage"] == {"input": 1200, "output": 80, "cost_eur": 0.0024}  # (1200*3 + 80*15) / 1e6 dollars, halved
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


# ---- recordings: hear back what the speech-to-text received -------------------------------------------------------
def tone(seconds, amplitude, noise=0.0, rate=16_000):
    t = np.arange(int(seconds * rate)) / rate
    rng = np.random.default_rng(0)
    return (amplitude * 32767 * np.sin(2 * np.pi * 220 * t) + noise * 32767 * rng.standard_normal(len(t))).astype(np.int16)


def test_levels_tell_a_good_signal_from_a_clipped_quiet_or_noisy_one():
    from pi_con.recordings import levels

    good = np.concatenate([tone(1, 0.001), tone(1, 0.3), tone(1, 0.001)])  # silence, voice, silence
    assert levels(good)["warning"] == "" and levels(good)["speech_db"] > -25
    assert "clipping" in levels(tone(1, 1.0))["warning"]
    assert "very quiet" in levels(tone(1, 0.001))["warning"]
    assert "noisy" in levels(tone(2, 0.05, noise=0.04))["warning"]
    assert levels(np.zeros(0, dtype=np.int16))["warning"] == "empty"


def test_store_saves_a_playable_wav_keeps_the_last_ones_and_checks_ids(tmp_path):
    import wave

    from pi_con.recordings import RecordingStore

    store = RecordingStore(tmp_path / "rec", keep=2)
    first = store.add(tone(1, 0.3), text="bonjour", local="bonjour")
    store.add(tone(1, 0.3), text="deux")
    store.add(tone(1, 0.3), text="trois")
    assert [r["text"] for r in store.list()] == ["trois", "deux"]  # newest first, the oldest was deleted
    assert store.wav(first["audio"]) is None
    newest = store.list()[0]
    with wave.open(str(store.wav(newest["audio"])), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()) == (16_000, 1, 2, 16_000)
    for bad in ("../../etc/passwd", "x", newest["audio"] + "/../x", ""):
        assert store.wav(bad) is None


def test_every_utterance_is_recorded_even_when_nothing_was_understood(monkeypatch, tmp_path):
    from pi_con.recordings import RecordingStore

    clock = [100.0]
    wl, bus, utterance = follow_up_listener(monkeypatch, clock, {})
    wl.recordings = RecordingStore(tmp_path, keep=5)
    sub, _ = bus.subscribe()
    wl._listen_requested.set()
    feed(wl, clock, 2)
    recs = []
    while not sub.empty():
        event = sub.get()
        if event["type"] == "recording":
            recs.append(event)
    assert len(recs) == 1 and recs[0]["text"] == "" and recs[0]["seconds"] > 0
    assert isinstance(recs[0]["id"], int)  # the bus id is untouched: the recording id is "audio"
    assert wl.recordings.wav(recs[0]["audio"]) is not None


def test_recording_test_keeps_raw_audio_and_transcribes_it_locally(monkeypatch, tmp_path):
    from pi_con.recordings import RecordingStore

    clock = [100.0]
    wl, bus, utterance = follow_up_listener(monkeypatch, clock, {})
    wl.transcriber.transcribe = lambda samples: f"{len(samples) // 16000} secondes"
    wl.recordings = RecordingStore(tmp_path, keep=5)
    sub, _ = bus.subscribe()
    assert wl.request_recording(3) and not wl.request_recording(0)
    feed(wl, clock, 5)
    recs = [e for e in iter(lambda: sub.get() if not sub.empty() else None, None) if e["type"] == "recording"]
    assert len(recs) == 1 and recs[0]["kind"] == "test" and recs[0]["text"].endswith("secondes") and 2.5 < recs[0]["seconds"] < 3.6


def test_recordings_off_means_no_test_and_no_files(monkeypatch):
    wl, _, _ = follow_up_listener(monkeypatch, [0.0], {})
    assert wl.recordings is None and wl.request_recording(5) is False


def test_ui_serves_recordings_and_starts_a_test(tmp_path):
    import urllib.error
    import urllib.request

    from pi_con.recordings import RecordingStore
    from pi_con.ui import start_ui

    store = RecordingStore(tmp_path, keep=3)
    meta = store.add(tone(1, 0.3), text="salut")
    asked = []
    server = start_ui(EventBus(), 0, host="127.0.0.1", recordings=store, on_record=lambda s: asked.append(s) or True)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        listing = json.loads(urllib.request.urlopen(base + "/recordings", timeout=5).read())
        assert listing[0]["audio"] == meta["audio"] and listing[0]["text"] == "salut"
        reply = urllib.request.urlopen(base + f"/recordings/{meta['audio']}.wav", timeout=5)
        assert reply.headers["Content-Type"] == "audio/wav" and reply.read()[:4] == b"RIFF"
        for bad in ("/recordings/..%2F..%2Fetc%2Fpasswd.wav", "/recordings/20260101-000000-000.wav"):
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + bad, timeout=5)
            assert error.value.code == 404
        assert urllib.request.urlopen(urllib.request.Request(base + "/recordings/test?seconds=4", method="POST"), timeout=5).status == 202
        assert asked == [4.0]
    finally:
        server.shutdown()
        server.server_close()


# ---- goodbye ends the conversation ---------------------------------------------------------------------------------
def test_a_goodbye_is_read_but_opens_no_follow_up_window(tmp_path):
    bus = EventBus()
    speaker, fakes = make(tmp_path, bus=bus)
    opened, played = [], threading.Event()
    speaker.after_reply = lambda: opened.append(1)
    speaker.play = lambda *a: (fakes.played.append(a), played.set())
    speaker.start()
    bus.publish("ai_reply", text="À bientôt !", provider="local", end=True)
    assert played.wait(5)
    import time
    time.sleep(0.3)
    assert opened == [] and fakes.synthesized == ["À bientôt !"]
    played.clear()
    bus.publish("ai_reply", text="Et toi ?", provider="local")  # the next answer is a normal one again
    assert played.wait(5)
    deadline = time.time() + 5
    while not opened and time.time() < deadline:
        time.sleep(0.05)
    assert opened == [1]


def test_the_ai_can_end_the_conversation_with_a_tool(tmp_path):
    bus = EventBus()
    speaker, fakes = make(tmp_path, enabled=False, bus=bus)  # voice off: the follow-up would open at once
    opened = []
    speaker.after_reply = lambda: opened.append(1)
    speaker.start()
    bus.publish("conversation_end", persist=False)
    bus.publish("ai_reply", text="Bonne soirée !", provider="openai")
    import time
    time.sleep(0.4)
    assert opened == []
    bus.publish("ai_reply", text="Autre chose ?", provider="openai")
    deadline = time.time() + 5
    while not opened and time.time() < deadline:
        time.sleep(0.05)
    assert opened == [1]


def test_au_revoir_skill_ends_the_conversation_and_end_must_be_boolean(tmp_path):
    from pi_con.skills import best_match, load_skills

    skills = load_skills("config/skills.json")
    for heard in ("au revoir jarvis", "bon allez au revoir"):
        match = best_match(heard, skills, 0.8)
        assert match.skill.name == "Au revoir" and match.skill.action["end"] is True
    assert best_match("merci", skills, 0.8).skill.action.get("end") is None  # thanks do not end it
    bus = EventBus()
    router = Router(bus, "config/skills.json", {}, None, 0.8)
    sub, _ = bus.subscribe(after_id=1 << 62)
    router.handle("au revoir jarvis")
    replies = [e for e in iter(lambda: sub.get(timeout=1) if not sub.empty() else None, None) if e["type"] == "ai_reply"]
    assert replies and replies[0]["end"] is True
    with pytest.raises(ValueError):
        parse_skills({"skills": [{"name": "x", "phrases": ["a b"], "action": {"type": "reply", "text": "ok", "end": "yes"}}]})


# ---- at most 5 recordings ------------------------------------------------------------------------------------------
def test_never_more_than_five_recordings_whatever_the_setting(tmp_path):
    from pi_con.recordings import RecordingStore

    store = RecordingStore(tmp_path, keep=50)
    for i in range(8):
        store.add(tone(0.2, 0.3), text=str(i))
    assert store.keep == 5 and len(list(tmp_path.glob("*.wav"))) == 5 and len(list(tmp_path.glob("*.json"))) == 5
    assert [r["text"] for r in store.list()] == ["7", "6", "5", "4", "3"]
    assert Settings("hey_jarvis", 0.5, None, ("true",)).recordings_keep == 5


# ---- pictures from the web -----------------------------------------------------------------------------------------
def wiki_pages(*pages):
    return {"query": {"pages": {str(i): dict(page, index=i + 1) for i, page in enumerate(pages)}}}


def test_image_comes_from_the_best_article_with_a_picture():
    from pi_con import images

    calls = []

    def fetch(url):
        calls.append(url)
        return wiki_pages({"title": "Sans image"}, {
            "title": "Pikachu", "fullurl": "https://fr.wikipedia.org/wiki/Pikachu",
            "thumbnail": {"source": "https://upload.wikimedia.org/a/pikachu.jpg"},
        })

    found = images.find_image("  pikachu ", fetch)
    assert found == {"url": "https://upload.wikimedia.org/a/pikachu.jpg", "title": "Pikachu", "source": "Wikipédia", "page": "https://fr.wikipedia.org/wiki/Pikachu"}
    assert len(calls) == 1 and calls[0].startswith("https://fr.wikipedia.org/w/api.php?") and "gsrsearch=pikachu" in calls[0]


def test_image_falls_back_to_english_then_commons_and_gives_up_quietly():
    from pi_con import images

    hosts = []

    def only_commons(url):
        hosts.append(url.split("/")[2])
        if "commons" in url:
            return wiki_pages({"title": "File:Tour Eiffel.jpg", "imageinfo": [{"thumburl": "https://upload.wikimedia.org/t.jpg", "mime": "image/jpeg"}]})
        return {"batchcomplete": ""}  # no result at all

    found = images.find_image("tour eiffel", only_commons)
    assert hosts == ["fr.wikipedia.org", "en.wikipedia.org", "commons.wikimedia.org"]
    assert (found["title"], found["source"]) == ("Tour Eiffel", "Wikimedia Commons")

    def offline(url):
        raise OSError("no network")

    assert images.find_image("pikachu", offline) is None
    assert images.find_image("   ", only_commons) is None


def test_only_https_wikimedia_images_are_ever_returned():
    from pi_con import images

    for bad in ("http://upload.wikimedia.org/a.jpg", "https://evil.example/a.jpg", "https://wikimedia.org.evil.example/a.jpg", "javascript:alert(1)", None):
        assert images.find_image("x", lambda url, bad=bad: wiki_pages({"title": "T", "thumbnail": {"source": bad}})) is None


def test_the_ai_can_show_a_picture_and_say_goodbye(monkeypatch):
    from pi_con import images
    from pi_con.tools import build_tools

    bus = EventBus()
    router = type("R", (), {"bus": bus, "run_skill": staticmethod(lambda s: "ok")})()
    monkeypatch.setattr(images, "find_image", lambda q: {"url": "https://upload.wikimedia.org/p.jpg", "title": q.title(), "source": "Wikipédia", "page": ""} if q != "rien" else None)
    tools = {t.name: t for t in build_tools(router, {}, None, lambda: (), "ai")}
    assert {"show_image", "end_conversation"} <= set(tools)
    assert not {"show_image", "end_conversation"} & {t.name for t in build_tools(router, {}, None, lambda: (), "mcp")}  # not for remote clients
    sub, _ = bus.subscribe()
    assert "'Pikachu'" in tools["show_image"].handler({"query": "pikachu"})
    assert "No picture found" in tools["show_image"].handler({"query": "rien"})
    with pytest.raises(ValueError):
        tools["show_image"].handler({"query": "  "})
    tools["end_conversation"].handler({})
    types = []
    while not sub.empty():
        types.append(sub.get())
    assert [e["type"] for e in types] == ["image", "conversation_end"] and types[0]["url"].endswith("p.jpg")


# ---- thinking markers, transcript engines, page reload ---------------------------------------------------------------
def test_thinking_ends_on_errors_too():
    from pi_con.ai.conversation import AiAssistant
    from pi_con.ai.providers import ProviderError
    from pi_con.ai.state import AiState

    class Broken:
        def run(self, *_a):
            raise ProviderError("no credit")

    bus = EventBus()
    live, _ = bus.subscribe()
    AiAssistant(bus, AiState(bus, {}, path="/dev/null"), lambda: [], provider_factory=lambda name: Broken()).ask("salut", "openai")
    flags = []
    while not live.empty():
        event = live.get()
        if event["type"] == "thinking":
            flags.append(event["active"])
    assert flags == [True, False]


def test_transcript_carries_what_each_engine_heard(monkeypatch):
    clock = [100.0]
    wl, bus, utterance = follow_up_listener(monkeypatch, clock, {20: "montre pikachu"})
    utterance.local_text, utterance.source = "montre pikachou", "openai"  # the local model garbled it, the cloud fixed it
    wl.request_followup(10)  # waits for the first word, like a real sentence that starts a little late
    feed(wl, clock, 3)
    transcript = next(e for e in bus.subscribe()[1] if e["type"] == "transcript")
    assert (transcript["text"], transcript["local"], transcript["engine"]) == ("montre pikachu", "montre pikachou", "openai")


def test_cloud_utterance_reports_who_wrote_the_text():
    import numpy as np

    from pi_con.stt import CloudUtterance

    class Local:
        local_text = ""

        def accept(self, _frame):
            self.local_text = "montre pikachou"
            return self.local_text, True

    cloud = CloudUtterance(Local(), lambda audio: "montre pikachu", timeout=2)
    text, done = "", False
    deadline = __import__("time").time() + 5
    while not done and __import__("time").time() < deadline:
        text, done = cloud.accept(np.zeros(1280, dtype=np.int16))
    assert (text, cloud.local_text, cloud.source) == ("montre pikachu", "montre pikachou", "openai")
    broken = CloudUtterance(Local(), lambda audio: 1 / 0, timeout=2)
    done = False
    while not done and __import__("time").time() < deadline:
        text, done = broken.accept(np.zeros(1280, dtype=np.int16))
    assert (text, broken.source) == ("montre pikachou", "local")  # the cloud failed: the local text is kept


def test_page_knows_the_boot_id_so_an_old_tab_reloads_after_a_restart():
    import urllib.request

    from pi_con.ui import start_ui

    server = start_ui(EventBus(), 0, host="127.0.0.1")
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        boot = urllib.request.urlopen(base + "/boot", timeout=5).read().decode()
        page = urllib.request.urlopen(base + "/", timeout=5).read().decode()
        assert boot.isdigit() and f'const BOOT_ID = "{boot}"' in page and "{{" not in page
    finally:
        server.shutdown()
        server.server_close()


def test_a_short_sentence_in_a_long_silence_is_not_called_noisy():
    from pi_con.recordings import levels

    quiet = tone(10, 0.0015)  # a 10 s follow-up window of room noise...
    clip = np.concatenate([quiet[:80_000], tone(1.5, 0.2), quiet[80_000:]])  # ...with 1.5 s of voice in it
    result = levels(clip)
    assert result["warning"] == "" and result["speech_db"] > -25 and result["speech_db"] - result["noise_db"] > 25


# ---- AI text / AI vocal, and the picture that must not be dismissed -------------------------------------------------
def test_ai_input_text_never_uploads_audio_and_is_saved(tmp_path):
    from pi_con.ai.state import AiState

    path = tmp_path / "ai.json"
    state = AiState(EventBus(), {}, path)
    assert (state.mode, state.input, state.cloud_stt()) == (False, "vocal", True)  # default: the former behaviour
    state.set_mode(True, "text")
    assert (state.input, state.cloud_stt(), state.snapshot()["input"]) == ("text", False, "text")
    state.set_mode(False)  # turning off keeps the last choice
    assert AiState(EventBus(), {}, path).input == "text"
    state.set_mode(True)  # turning on without a choice keeps it too
    assert state.input == "text"
    state.set_mode(True, "vocal")
    assert state.cloud_stt() is True
    state.set_mode(True, "nonsense")  # unknown values are ignored
    assert state.input == "vocal"


def test_defaults_and_old_saved_choice_map_to_the_new_input(tmp_path):
    from pi_con.ai.state import AiState

    assert AiState(EventBus(), {"cloud_stt": False}, tmp_path / "a.json").input == "text"  # settings.json default
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"mode": True, "options": {"cloud_stt": False}}))  # saved before the choice existed
    migrated = AiState(EventBus(), {}, old)
    assert (migrated.mode, migrated.input) == (True, "text")


def test_voice_commands_choose_text_or_vocal_and_the_longer_phrase_wins(tmp_path):
    from pi_con.ai.conversation import AiAssistant
    from pi_con.ai.state import AiState
    from pi_con.skills import best_match, load_skills

    skills = load_skills("config/skills.json")
    assert best_match("mode ia texte", skills, 0.8).skill.action["mode"] == "text"
    assert best_match("mode ia vocal", skills, 0.8).skill.action["mode"] == "vocal"
    assert best_match("mode ia", skills, 0.8).skill.action["mode"] == "on"
    bus = EventBus()
    state = AiState(bus, {}, tmp_path / "ai.json")
    router = Router(bus, "config/skills.json", {}, None, 0.8, ai=AiAssistant(bus, state, lambda: []))
    router.handle("mode ia texte")
    assert (state.mode, state.input) == (True, "text")
    router.handle("mode ia vocal")
    assert (state.mode, state.input) == (True, "vocal")
    router.handle("mode ia")  # "on" keeps the input
    assert state.input == "vocal"


def test_ai_endpoint_picks_the_input(tmp_path):
    import urllib.error
    import urllib.request

    from pi_con.ai.state import AiState
    from pi_con.ui import start_ui

    bus = EventBus()
    state = AiState(bus, {}, tmp_path / "ai.json")
    server = start_ui(bus, 0, host="127.0.0.1", ai_state=state)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        def post(path):
            try:
                return urllib.request.urlopen(urllib.request.Request(base + path, method="POST"), timeout=5).status
            except urllib.error.HTTPError as error:
                return error.code
        assert post("/ai/mode?on=1&input=text") == 200 and (state.mode, state.input) == (True, "text")
        assert post("/ai/mode?on=1&input=vocal") == 200 and state.input == "vocal"
        assert post("/ai/mode?on=0") == 200 and (state.mode, state.input) == (False, "vocal")
        assert post("/ai/mode?on=1&input=carrier-pigeon") == 400 and state.mode is False
    finally:
        server.shutdown()
        server.server_close()


def test_removing_the_picture_is_local_even_in_ai_mode_and_needle_can_do_it(tmp_path):
    from pi_con.fallback import NeedleFallback
    from pi_con.skills import best_match, load_skills

    skills = load_skills("config/skills.json")
    bus = EventBus()

    class Ai:  # AI mode on: this stand-in fails the test if the sentence is sent to the model
        state = type("S", (), {"mode": True})()

        def ask(self, _text):
            raise AssertionError("sent to the model")

    router = Router(bus, "config/skills.json", {}, None, 0.8, ai=Ai())
    sub, _ = bus.subscribe(after_id=1 << 62)
    for sentence in ("enlève l'image", "retire la photo", "ferme l'image"):
        assert router.handle(sentence)
    kinds = []
    while not sub.empty():
        kinds.append(sub.get()["type"])
    assert kinds.count("image_hide") == 3 and "camera" not in kinds
    # Needle gets a tool for it too, because the skill opts in with "fallback": true
    seen = {}
    fallback = NeedleFallback({}, agent_factory=lambda tools: seen.update(tools={t.__name__: t for t in tools}) or type("A", (), {"run": lambda self, text: seen["tools"]["enlever_l_image"]()})())
    chosen = fallback.interpret("peux-tu m'enlever cette image s'il te plaît", skills)
    assert chosen is not None and chosen.action["type"] == "hide_picture" and "enlever_l_image" in seen["tools"]


def test_a_camera_hide_does_not_touch_the_picture_and_the_model_has_its_own_tool(monkeypatch):
    from pi_con.tools import build_tools

    bus = EventBus()
    skills = parse_skills({"skills": [{"name": "Fermer", "phrases": ["ferme la vidéo"], "action": {"type": "camera_hide"}}]})
    path = None
    router = type("R", (), {"bus": bus, "run_skill": staticmethod(lambda s: "ok")})()
    tools = {t.name: t for t in build_tools(router, {"rabbit": "camera.rabbit"}, None, lambda: skills, "ai")}
    sub, _ = bus.subscribe()
    tools["hide_camera"].handler({})
    assert "image_hide" not in [e["type"] for e in iter(lambda: sub.get() if not sub.empty() else None, None)]  # camera tool: no picture event
    assert "hide_image" in tools and "show_image" in tools
    tools["hide_image"].handler({})
    assert [e["type"] for e in iter(lambda: sub.get() if not sub.empty() else None, None)] == ["image_hide"]
