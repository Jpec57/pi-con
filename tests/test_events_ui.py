import json
import urllib.request
import wave

import numpy as np
import pytest

from pi_con.audio import read_wav_frames
from pi_con.events import EventBus
from pi_con.ui import start_ui


def test_history_replays_only_newer_events_and_skips_transient():
    bus = EventBus()
    first = bus.publish("wake", word="hey_jarvis", score=0.9)
    bus.publish("score", persist=False, score=0.4)
    second = bus.publish("transcript", text="allume la lumière")
    _, backlog = bus.subscribe()
    assert [e["type"] for e in backlog] == ["wake", "transcript"]
    _, backlog = bus.subscribe(after_id=first["id"])
    assert [e["id"] for e in backlog] == [second["id"]]


def test_live_subscriber_receives_events_and_full_queue_never_blocks():
    bus = EventBus()
    live, _ = bus.subscribe()
    bus.publish("wake", word="x", score=1.0)
    assert live.get_nowait()["type"] == "wake"
    for _ in range(500):  # more than the queue holds: must not raise or block
        bus.publish("score", persist=False, score=0.0)


@pytest.fixture
def server():
    bus = EventBus()
    srv = start_ui(bus, 0, host="127.0.0.1")
    yield bus, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_serves_page_and_health(server):
    _, url = server
    with urllib.request.urlopen(url + "/", timeout=5) as page:
        assert page.headers["Content-Type"].startswith("text/html")
        assert b"Pi-Con" in page.read()
    with urllib.request.urlopen(url + "/healthz", timeout=5) as health:
        assert health.read() == b"ok"


def read_events(response, count):
    events = []
    while len(events) < count:
        line = response.readline().decode()
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def test_sse_streams_history_then_live_and_honours_last_event_id(server):
    bus, url = server
    first = bus.publish("wake", word="hey_jarvis", score=0.9)
    with urllib.request.urlopen(url + "/events", timeout=5) as stream:
        assert stream.headers["Content-Type"] == "text/event-stream"
        assert read_events(stream, 1)[0]["id"] == first["id"]
        bus.publish("transcript", text="bonjour")
        assert read_events(stream, 1)[0]["text"] == "bonjour"

    request = urllib.request.Request(url + "/events", headers={"Last-Event-ID": str(first["id"])})
    with urllib.request.urlopen(request, timeout=5) as stream:
        assert read_events(stream, 1)[0]["text"] == "bonjour"  # the older wake event is not repeated


def write_wav(path, samples, rate=16_000, channels=1):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(np.asarray(samples, dtype=np.int16).tobytes())


def test_read_wav_frames_pads_last_frame(tmp_path):
    path = tmp_path / "a.wav"
    write_wav(path, np.ones(2000))
    frames = read_wav_frames(path, 16_000, 1280)
    assert [len(f) for f in frames] == [1280, 1280]
    assert frames[1][720:].sum() == 0 and frames[1][:720].sum() == 720


def test_read_wav_frames_rejects_wrong_format(tmp_path):
    path = tmp_path / "b.wav"
    write_wav(path, np.ones(100), rate=44_100)
    with pytest.raises(ValueError, match="ffmpeg"):
        read_wav_frames(path, 16_000, 1280)
