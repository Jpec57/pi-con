import contextlib
import json

import numpy as np
import pytest

from pi_con import listener
from pi_con.config import Settings
from pi_con.events import EventBus


class FakeModel:
    def __init__(self, scores):
        self.scores = iter(scores)
        self.resets = 0

    def predict(self, _frame):
        try:
            return {"hey_jarvis": np.float32(next(self.scores))}  # like the real model
        except StopIteration:
            raise KeyboardInterrupt

    def reset(self):
        self.resets += 1


class fake_stream:  # noqa: N801 (stands in for sd.InputStream)
    def __init__(self, **_kwargs):
        self.closed = False

    def start(self):
        pass

    def abort(self):
        pass

    def close(self):
        self.closed = True


def run(monkeypatch, scores, cooldown, bus=None):
    model = FakeModel(scores)
    requested = []
    monkeypatch.setattr(listener, "load_model", lambda name: requested.append(name) or model)
    monkeypatch.setattr(listener.sd, "InputStream", fake_stream)
    settings = Settings("hey_jarvis", 0.5, None, ("true",), cooldown_seconds=cooldown)
    wakes = []
    wl = listener.WakeWordListener(settings, lambda: wakes.append(1), bus=bus)
    for _ in range(len(scores) + 1):  # one extra frame makes the fake model stop the loop
        wl.frames.put(np.zeros(1280, dtype=np.int16))
    with pytest.raises(KeyboardInterrupt):
        wl.run_forever()
    return wakes, model, requested


def test_loads_only_configured_model(monkeypatch):
    _, _, requested = run(monkeypatch, [0.0], cooldown=2.0)
    assert requested == ["hey_jarvis"]


def test_sustained_score_triggers_once_within_cooldown(monkeypatch):
    wakes, model, _ = run(monkeypatch, [0.1, 0.9, 0.95, 0.9, 0.1], cooldown=60.0)
    assert len(wakes) == 1
    assert model.resets == 1


def test_no_trigger_below_threshold(monkeypatch):
    wakes, _, _ = run(monkeypatch, [0.1, 0.4, 0.49], cooldown=2.0)
    assert wakes == []


def test_publishes_json_serialisable_events(monkeypatch):
    bus = EventBus()
    live, _ = bus.subscribe()
    run(monkeypatch, [0.1, 0.9], cooldown=2.0, bus=bus)
    events = []
    while not live.empty():
        events.append(live.get_nowait())
    assert [e["type"] for e in events][:2] == ["status", "score"]
    assert events[-1]["type"] == "wake" and events[-1]["score"] == 0.9
    json.dumps(events)  # raises on numpy types, which is what broke the browser stream


def test_to_model_rate_resamples_48k_block_to_one_frame():
    import numpy as np

    from pi_con.listener import to_model_rate

    tone = (np.sin(2 * np.pi * 440 * np.arange(3840) / 48_000) * 10_000).astype(np.int16)
    out = to_model_rate(tone, 48_000, 16_000)
    assert out.dtype == np.int16
    assert len(out) == 1280
    assert 6_000 < np.abs(out[100:-100]).max() < 12_000


def test_to_model_rate_handles_44100():
    import numpy as np

    from pi_con.listener import to_model_rate

    assert len(to_model_rate(np.zeros(3528, dtype=np.int16), 44_100, 16_000)) == 1280


class FakeUtterance:
    def __init__(self, texts):
        self.texts = iter(texts)

    def accept(self, _frame):
        text, done = next(self.texts)
        return text, done


class FakeTranscriber:
    def __init__(self, texts):
        self.utterance = FakeUtterance(texts)

    def start(self):
        return self.utterance


def test_wake_word_starts_transcription_and_publishes_partial_then_final(monkeypatch):
    model = FakeModel([0.9, 0.0])  # predict is skipped while capturing; the 3rd call stops the loop
    monkeypatch.setattr(listener, "load_model", lambda name: model)
    monkeypatch.setattr(listener.sd, "InputStream", fake_stream)
    settings = Settings("hey_jarvis", 0.5, None, ("true",), cooldown_seconds=0)
    bus = EventBus()
    sub, _ = bus.subscribe()
    texts = [("allume", False), ("allume la lumière", False), ("allume la lumière", True)]
    wl = listener.WakeWordListener(settings, lambda: None, bus=bus, transcriber=FakeTranscriber(texts))
    wl.frames.put(np.zeros(1280, dtype=np.int16))  # wake word frame
    for _ in range(3):
        wl.frames.put(np.zeros(1280, dtype=np.int16))
    for _ in range(2):  # back in wake mode: the fake model stops the loop
        wl.frames.put(np.zeros(1280, dtype=np.int16))
    with pytest.raises(KeyboardInterrupt):
        wl.run_forever()
    events = []
    while not sub.empty():
        events.append(sub.get_nowait())
    kinds = [(e["type"], e.get("text") or e.get("active")) for e in events if e["type"] in ("partial", "transcript", "capture")]
    assert kinds == [
        ("capture", True), ("partial", "allume"), ("partial", "allume la lumière"),
        ("transcript", "allume la lumière"), ("capture", False),
    ]
    assert model.resets == 2  # after the wake word and after the utterance


def test_request_listen_is_refused_without_stt():
    wl = listener.WakeWordListener.__new__(listener.WakeWordListener)
    wl.transcriber = None
    assert wl.request_listen() is False


def test_stalled_mic_is_reopened_and_ui_can_request_a_restart(monkeypatch):
    model = FakeModel([0.0])
    monkeypatch.setattr(listener, "load_model", lambda name: model)
    streams = []
    monkeypatch.setattr(listener.sd, "InputStream", lambda **kw: streams.append(fake_stream(**kw)) or streams[-1])
    monkeypatch.setattr(listener.sd, "query_devices", lambda *a, **k: {"name": "fake mic", "default_samplerate": 16000})
    settings = Settings("hey_jarvis", 0.5, None, ("true",), cooldown_seconds=0)
    wl = listener.WakeWordListener(settings, lambda: None)
    wl._start_stream()
    assert wl.debug.stream == "running" and wl.debug.device == "fake mic"

    wl.debug.last_frame -= listener.STALL_SECONDS + 1  # pretend no block arrived for a while
    wl._check_audio(0)
    assert (wl.debug.stalls, wl.debug.stream_restarts, len(streams)) == (1, 1, 2)
    assert streams[0].closed

    assert wl.request_audio_restart()
    wl._check_audio(0)
    assert (wl.debug.stream_restarts, len(streams)) == (2, 3)


def test_process_restarts_itself_when_audio_never_comes_back(monkeypatch):
    monkeypatch.setattr(listener, "load_model", lambda name: FakeModel([]))
    monkeypatch.setattr(listener.sd, "InputStream", fake_stream)
    monkeypatch.setattr(listener.sd, "query_devices", lambda *a, **k: {"name": "m", "default_samplerate": 16000})
    exits = []
    monkeypatch.setattr(listener.os, "_exit", lambda code: exits.append(code))
    wl = listener.WakeWordListener(Settings("hey_jarvis", 0.5, None, ("true",)), lambda: None)
    wl._start_stream()
    for _ in range(listener.MAX_STALL_STREAK + 1):
        wl.debug.last_frame -= listener.STALL_SECONDS + 1
        wl._check_audio(0)
    assert exits == [3]
