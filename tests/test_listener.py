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


@contextlib.contextmanager
def fake_stream(**_kwargs):
    yield


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
