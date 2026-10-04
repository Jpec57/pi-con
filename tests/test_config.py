import json

import pytest

from pi_con.config import load_settings


def test_load_settings(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"wakeword": "hey_jarvis", "command": ["echo", "wake"]}))
    settings = load_settings(path)
    assert settings.wakeword == "hey_jarvis"
    assert settings.frame_samples == 1280


def test_requires_command(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"wakeword": "hey_jarvis"}')
    with pytest.raises(ValueError, match="command"):
        load_settings(path)


def test_wake_phrase_defaults_to_prettified_wakeword(tmp_path):
    from pi_con.config import default_phrase

    assert default_phrase("hey_jarvis") == "Hey Jarvis"
    assert default_phrase("/models/ok_maison.onnx") == "Ok Maison"
