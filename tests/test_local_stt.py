import json
import urllib.request

import numpy as np
import pytest

from pi_con import local_stt
from pi_con.events import EventBus
from pi_con.local_stt import ManagedTranscriber
from pi_con.local_stt import _OfflineUtterance
from pi_con.ui import start_ui


class FakeWorker:
    events = []
    fail_ids = set()
    timeout_ids = set()

    def __init__(self, spec, language, *_args):
        self.model_id = spec.id
        self.language = language
        self.stopped = False

    def start(self):
        self.events.append(("start", self.model_id))
        if self.model_id in self.timeout_ids:
            raise TimeoutError("simulated model load timeout")
        if self.model_id in self.fail_ids:
            raise RuntimeError("simulated model load failure")

    def request(self, payload, timeout=None):
        if payload["command"] == "new":
            return {"ok": True}
        if payload["command"] == "transcribe":
            return {"ok": True, "text": "allume la lumière", "elapsed_s": 0.25,
                    "cpu_seconds": 0.1, "rss_peak_bytes": 1234,
                    "mem_available_bytes": 2_000_000_000, "swap_used_bytes": 0}
        return {"ok": True, "text": "bonjour", "finished": True}

    def stop(self):
        if not self.stopped:
            self.stopped = True
            self.events.append(("stop", self.model_id))


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(local_stt, "_requirements", lambda _spec: ([], None))
    FakeWorker.events = []
    FakeWorker.fail_ids = set()
    FakeWorker.timeout_ids = set()
    return ManagedTranscriber("zip", state_path=tmp_path / "active.json", memory_provider=lambda: 2_000_000_000,
                              worker_factory=FakeWorker, bus=EventBus())


def test_switch_is_queued_then_stops_old_before_starting_new(manager):
    manager.start_initial()
    old = manager.worker
    accepted, _ = manager.request_switch("canary-fr")
    assert accepted and manager.active_id == "zipformer-fr" and manager.worker is old
    assert manager.apply_pending() is True
    assert manager.active_id == "canary-fr" and old.stopped
    assert FakeWorker.events == [
        ("start", "zipformer-fr"), ("stop", "zipformer-fr"), ("start", "canary-fr")
    ]
    assert json.loads(manager.state_path.read_text()) == {"model": "canary-fr", "language": "fr"}


def test_failed_model_load_restores_zipformer_without_saving_failed_choice(manager):
    manager.start_initial()
    FakeWorker.fail_ids = {"orukeet-fr"}
    assert manager.request_switch("orukeet-fr")[0]
    assert manager.apply_pending() is False
    assert manager.active_id == "zipformer-fr"
    assert json.loads(manager.state_path.read_text()) == {"model": "zipformer-fr", "language": "fr"}
    assert FakeWorker.events[-3:] == [
        ("stop", "zipformer-fr"), ("start", "orukeet-fr"), ("start", "zipformer-fr")
    ]


def test_model_load_timeout_is_reported_and_zipformer_fallback_is_started(manager):
    manager.start_initial()
    FakeWorker.timeout_ids = {"canary-fr"}
    assert manager.request_switch("canary-fr")[0]
    assert manager.apply_pending() is False
    assert manager.active_id == "zipformer-fr"
    assert "simulated model load timeout" in manager.last_error
    assert FakeWorker.events[-2:] == [("start", "canary-fr"), ("start", "zipformer-fr")]


def test_low_memory_refuses_switch_before_stopping_active_zipformer(manager):
    manager.start_initial()
    manager.memory_provider = lambda: 800 * 1024 * 1024
    manager.bus = None
    accepted, reason = manager.request_switch("canary-fr")
    assert accepted is False
    assert "safety reserve" in reason
    assert manager.has_pending_switch() is False
    assert manager.active_id == "zipformer-fr"
    assert manager.worker is not None
    assert FakeWorker.events == [("start", "zipformer-fr")]


def test_language_validation_and_unknown_engine_are_rejected(manager):
    assert manager.request_switch("no-such-model")[0] is False
    assert manager.request_switch("vosk-fr", "en")[0] is False
    assert manager.request_switch("vosk-en", "en")[0] is True


def test_offline_decode_is_deferred_until_listener_can_pause_capture(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(local_stt.time, "monotonic", lambda: clock[0])
    class Engine:
        calls = 0
        def transcribe(self, _samples):
            self.calls += 1
            return "phrase finale"
    engine = Engine()
    utterance = _OfflineUtterance(engine)
    assert utterance.accept(np.full(1280, 1000, dtype=np.int16)) == ("", False)
    clock[0] += 1.1
    text, finished = utterance.accept(np.zeros(1280, dtype=np.int16))
    assert (text, finished, utterance.needs_finalization, engine.calls) == ("", True, True, 0)
    assert utterance.finish() == ("phrase finale", True)
    assert engine.calls == 1 and not utterance.needs_finalization


def test_worker_timeout_schedules_fallback_at_boundary(manager, monkeypatch):
    manager.start_initial()
    utterance = manager.start()

    def timeout(_payload, timeout=None):
        raise TimeoutError("simulated inference timeout")

    manager.worker.request = timeout
    text, finished = utterance.accept(np.zeros(1280, dtype=np.int16))
    assert (text, finished) == ("", True)
    assert manager.has_pending_switch()
    assert manager.active_id == "zipformer-fr"


def test_diagnostic_transcribes_local_recording_without_command_event(tmp_path, manager, monkeypatch):
    import wave
    from pi_con.recordings import RecordingStore
    from pi_con.local_stt import diagnostic_from_recording

    manager.start_initial()
    store = RecordingStore(tmp_path / "recordings", keep=5)
    meta = store.add(np.zeros(16000, dtype=np.int16), text="old result", kind="test")
    live, _ = manager.bus.subscribe()
    result = diagnostic_from_recording(manager, store, meta["audio"], "allume la lumière")
    assert result["text"] == "allume la lumière"
    assert result["scores"]["wer"] == 0 and result["scores"]["cer"] == 0
    assert result["cpu_seconds"] == 0.1 and result["rss_peak_bytes"] == 1234
    events = []
    while not live.empty():
        events.append(live.get_nowait())
    assert all(event["type"] != "transcript" for event in events)
    assert "text" not in manager.snapshot()["last_transcription"]
    assert (store.folder.parent / "stt-diagnostics" / "results.jsonl").is_file()


def test_local_model_endpoint_is_separate_from_openai_stt_model(manager, tmp_path):
    class AiState:
        store = None
        def settings_view(self):
            return {"providers": [], "stt_model": "cloud-model", "default_stt_model": "cloud-default"}
        def set_options(self, *_args, **_kwargs):
            raise AssertionError("local STT endpoint must not change cloud STT")

    manager.start_initial()
    server = start_ui(EventBus(), 0, host="127.0.0.1", ai_state=AiState(), local_stt=manager)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/settings") as response:
            settings = json.load(response)
        assert settings["stt_model"] == "cloud-model"
        assert settings["local_stt"]["active_model"] == "zipformer-fr"
        request = urllib.request.Request(base + "/settings/local-stt", data=json.dumps({"model": "canary-fr"}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request) as response:
            payload = json.load(response)
        assert response.status == 202 and payload["local_stt"]["pending"]
        assert manager.active_id == "zipformer-fr" and manager.has_pending_switch()
    finally:
        server.shutdown()
        server.server_close()
        manager.close()
