import numpy as np
from types import SimpleNamespace

from pi_con import recordings


def test_retains_latest_recordings_with_frozen_clock_and_after_restart(tmp_path, monkeypatch):
    clock = SimpleNamespace(time=lambda: 1_700_000_000.0)
    monkeypatch.setattr(recordings, "time", clock)
    samples = np.zeros(3200, dtype=np.int16)
    store = recordings.RecordingStore(tmp_path, keep=5)
    for i in range(8):
        store.add(samples, text=str(i))

    assert [item["text"] for item in store.list()] == ["7", "6", "5", "4", "3"]
    assert len(list(tmp_path.glob("*.wav"))) == len(list(tmp_path.glob("*.json"))) == 5

    # Existing names remain usable and ordering survives a restart and a clock rollback.
    store = recordings.RecordingStore(tmp_path, keep=5)
    previous = store.list()[0]["audio"]
    assert store.wav(previous).is_file()
    clock.time = lambda: 1_699_999_000.0
    new = store.add(samples, text="8")
    assert new["audio"] > previous
    assert recordings.ID_PATTERN.fullmatch(new["audio"])
    assert [item["text"] for item in store.list()] == ["8", "7", "6", "5", "4"]
