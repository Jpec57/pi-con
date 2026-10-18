import json
import urllib.error
import urllib.request

import pytest

from pi_con.config import Camera, load_settings
from pi_con.events import EventBus
from pi_con.ha import HomeAssistant, HomeAssistantError
from pi_con.ui import start_ui


def test_settings_load_cameras_skills_and_token(tmp_path, monkeypatch):
    monkeypatch.delenv("HA_TOKEN", raising=False)
    (tmp_path / ".env").write_text("HA_TOKEN=secret\n", encoding="utf-8")
    (tmp_path / "config").mkdir()
    config = tmp_path / "config" / "settings.json"
    config.write_text(json.dumps({
        "wakeword": "hey_jarvis", "command": ["true"], "home_assistant_url": "http://ha:8123/",
        "cameras": [{"name": "rabbit", "entity": "camera.rabbit"}], "skill_threshold": 0.7,
    }), encoding="utf-8")
    settings = load_settings(config)
    assert settings.ha_token == "secret" and settings.ha_url == "http://ha:8123"
    assert settings.cameras == (Camera("rabbit", "camera.rabbit"),)
    assert settings.skills_file == "config/skills.json" and settings.skill_threshold == 0.7
    monkeypatch.delenv("HA_TOKEN")


class FakeHA(HomeAssistant):
    def __init__(self):
        super().__init__("http://ha.invalid", "token")
        self.broken = False

    def camera_stream_path(self, entity):
        if self.broken:
            raise HomeAssistantError("Home Assistant is unreachable")
        return f"/api/hls/abc/{entity}/master_playlist.m3u8"


@pytest.fixture
def ui():
    ha = FakeHA()
    server = start_ui(EventBus(), 0, host="127.0.0.1", ha=ha, cameras={"rabbit": "camera.rabbit"})
    yield ha, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_stream_endpoint_returns_proxied_hls_url(ui):
    _, url = ui
    status, body = get(url + "/camera/rabbit/stream")
    assert status == 200 and json.loads(body)["url"] == "/ha/api/hls/abc/camera.rabbit/master_playlist.m3u8"


def test_unknown_camera_and_ha_down(ui):
    ha, url = ui
    assert get(url + "/camera/garage/stream")[0] == 404
    ha.broken = True
    status, body = get(url + "/camera/rabbit/stream")
    assert status == 502 and b"unreachable" in body


def test_proxy_only_relays_hls_paths(ui):
    _, url = ui
    assert get(url + "/ha/api/states")[0] == 404  # not /ha/api/hls/: never an open proxy to Home Assistant
    with pytest.raises(HomeAssistantError):
        HomeAssistant("http://x", "t").open_hls("/api/hls/../config")


def test_serves_hls_player(ui):
    _, url = ui
    status, body = get(url + "/static/hls.min.js")
    assert status == 200 and len(body) > 100_000


def test_device_list_filters_plumbing_and_marks_cameras():
    from pi_con.ha import device_list
    states = [
        {"entity_id": "sun.sun", "state": "above_horizon", "attributes": {}},
        {"entity_id": "sensor.sun_next_dawn", "state": "x", "attributes": {}},
        {"entity_id": "light.salon", "state": "off", "attributes": {"friendly_name": "Salon"}},
        {"entity_id": "camera.rabbit", "state": "idle", "attributes": {"friendly_name": "Rabbit"}},
        {"entity_id": "camera.other", "state": "idle", "attributes": {}},
    ]
    devices = device_list(states, {"rabbit": "camera.rabbit"})
    assert [d["id"] for d in devices] == ["camera.other", "camera.rabbit", "light.salon"]
    assert [d["camera"] for d in devices] == [None, "rabbit", None]


def test_devices_endpoint_reports_ha_outage_without_failing(ui):
    ha, url = ui
    ha.states = lambda: [{"entity_id": "camera.rabbit", "state": "idle", "attributes": {"friendly_name": "Rabbit"}}]
    status, body = get(url + "/devices")
    assert status == 200 and json.loads(body)["devices"][0]["camera"] == "rabbit"

    def down():
        raise HomeAssistantError("Home Assistant is unreachable")
    ha.states = down
    status, body = get(url + "/devices")
    assert status == 200 and json.loads(body) == {"devices": [], "error": "Home Assistant is unreachable"}
