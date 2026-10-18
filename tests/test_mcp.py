import json
import urllib.error
import urllib.request

import pytest

from pi_con.events import EventBus
from pi_con.mcp import build_tools, start_mcp
from pi_con.router import Router
from pi_con.skills import parse_skills

TOKEN = "t" * 32
SKILLS = parse_skills({"skills": [
    {"name": "Salon allumé", "phrases": ["allume le salon"], "mcp": True, "description": "Turn on the living room light.",
     "action": {"type": "ha_service", "service": "light.turn_on", "entity_id": "light.salon"}},
    {"name": "Redémarrer", "phrases": ["relance"], "action": {"type": "command", "command": ["true"]}},  # not exposed
]})


class FakeHA:
    def __init__(self):
        self.calls = []

    def states(self):
        return [{"entity_id": "camera.rabbit", "state": "idle", "attributes": {"friendly_name": "Rabbit"}}]

    def call_service(self, service, data):
        self.calls.append((service, data))


@pytest.fixture
def mcp(tmp_path):
    bus = EventBus()
    live, _ = bus.subscribe()
    ha = FakeHA()
    cameras = {"rabbit": "camera.rabbit"}
    router = Router(bus, tmp_path / "none.json", cameras, ha, 0.8)
    server = start_mcp(0, TOKEN, lambda: build_tools(router, cameras, ha, lambda: SKILLS, "mcp"))
    yield f"http://127.0.0.1:{server.server_address[1]}", live, ha
    server.shutdown()
    server.server_close()


def post(url, payload, raw=None):
    request = urllib.request.Request(url, data=raw if raw is not None else json.dumps(payload).encode(), method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            body = reply.read()
            return reply.status, json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        return error.code, None


def rpc(base, method, params=None, id_=1):
    return post(f"{base}/mcp/{TOKEN}", {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})


def test_wrong_or_missing_token_is_a_plain_404(mcp):
    base, _, _ = mcp
    assert post(f"{base}/mcp/nope", {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0] == 404
    assert post(f"{base}/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0] == 404
    assert post(f"{base}/", {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0] == 404
    assert post(f"{base}/mcp/{TOKEN}x", {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0] == 404


def test_initialize_negotiates_version_and_declares_tools(mcp):
    base, _, _ = mcp
    status, reply = rpc(base, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}})
    assert status == 200 and reply["result"]["protocolVersion"] == "2025-06-18"
    assert "tools" in reply["result"]["capabilities"]
    assert rpc(base, "initialize", {"protocolVersion": "1999-01-01"})[1]["result"]["protocolVersion"] == "2025-11-25"


def test_notification_gets_202(mcp):
    base, _, _ = mcp
    status, body = post(f"{base}/mcp/{TOKEN}", {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202 and body is None


def test_tool_list_is_an_allowlist(mcp):
    base, _, _ = mcp
    names = [t["name"] for t in rpc(base, "tools/list")[1]["result"]["tools"]]
    assert names == ["show_camera", "hide_camera", "list_devices", "salon_allume"]  # 'Redémarrer' is not opted in


def test_show_and_hide_camera_drive_the_ui(mcp):
    base, live, _ = mcp
    reply = rpc(base, "tools/call", {"name": "show_camera", "arguments": {"camera": "rabbit"}})[1]["result"]
    assert reply["isError"] is False and "rabbit" in reply["content"][0]["text"]
    event = live.get_nowait()
    assert (event["type"], event["action"], event["name"]) == ("camera", "show", "rabbit")
    rpc(base, "tools/call", {"name": "hide_camera"})
    assert live.get_nowait()["action"] == "hide"


def test_bad_arguments_are_tool_errors_not_crashes(mcp):
    base, live, _ = mcp
    reply = rpc(base, "tools/call", {"name": "show_camera", "arguments": {"camera": "garage"}})[1]["result"]
    assert reply["isError"] is True and "unknown camera" in reply["content"][0]["text"] and live.empty()


def test_opted_in_skill_runs_and_unknown_tool_is_an_rpc_error(mcp):
    base, _, ha = mcp
    assert rpc(base, "tools/call", {"name": "salon_allume"})[1]["result"]["isError"] is False
    assert ha.calls == [("light.turn_on", {"entity_id": "light.salon"})]
    assert rpc(base, "tools/call", {"name": "relance"})[1]["error"]["code"] == -32602


def test_list_devices_returns_names_and_state_only(mcp):
    base, _, _ = mcp
    text = rpc(base, "tools/call", {"name": "list_devices"})[1]["result"]["content"][0]["text"]
    assert json.loads(text) == [{"name": "Rabbit", "type": "camera", "state": "idle"}]


def test_malformed_and_unknown_requests(mcp):
    base, _, _ = mcp
    assert post(f"{base}/mcp/{TOKEN}", None, raw=b"{nope")[0] == 400
    assert rpc(base, "resources/list")[1]["error"]["code"] == -32601
    request = urllib.request.Request(f"{base}/mcp/{TOKEN}", method="GET")
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)
    assert error.value.code == 405


def test_short_token_is_refused_and_the_endpoint_stays_off():
    server = start_mcp(0, "short", lambda: [])
    try:
        assert server.token == ""
        with pytest.raises(ValueError, match="24"):
            server.set_token("short")
        assert post(f"http://127.0.0.1:{server.server_address[1]}/mcp/", {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0] == 404
        server.set_token("x" * 30)  # a good token can be set at runtime
        assert post(f"http://127.0.0.1:{server.server_address[1]}/mcp/{'x' * 30}", {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0] == 200
    finally:
        server.shutdown()
        server.server_close()
