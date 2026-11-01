import json
import os
import stat
import urllib.error
import urllib.request

import pytest

from pi_con.ai.conversation import AiAssistant
from pi_con.ai.state import AiState
from pi_con.events import EventBus
from pi_con.mcp import start_mcp
from pi_con.secrets import SecretStore
from pi_con.ui import start_ui

AI = {"default_provider": "anthropic", "providers": {"anthropic": {"model": "m-a"}, "openai": {"model": "m-o", "stt_model": "t-default"}}}
KEY = "sk-ant-" + "a" * 30
OTHER = "sk-proj-" + "b" * 30


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MCP_TOKEN"):
        monkeypatch.delenv(name, raising=False)


# ---- the store -------------------------------------------------------------------------------------------------
def test_ui_value_wins_and_env_is_the_default(tmp_path, monkeypatch):
    store = SecretStore(tmp_path / "s.json")
    assert (store.get("OPENAI_API_KEY"), store.source("OPENAI_API_KEY")) == ("", "none")
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    assert (store.get("OPENAI_API_KEY"), store.source("OPENAI_API_KEY")) == ("from-env", "env")
    store.set("OPENAI_API_KEY", "from-ui")
    assert (store.get("OPENAI_API_KEY"), store.source("OPENAI_API_KEY")) == ("from-ui", "ui")
    store.clear("OPENAI_API_KEY")  # removing the UI value goes back to the .env default
    assert (store.get("OPENAI_API_KEY"), store.source("OPENAI_API_KEY")) == ("from-env", "env")


def test_saved_values_persist_in_a_private_file(tmp_path):
    path = tmp_path / "state" / "s.json"
    SecretStore(path).set("ANTHROPIC_API_KEY", KEY)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert SecretStore(path).get("ANTHROPIC_API_KEY") == KEY
    assert not list(path.parent.glob("*.tmp"))


def test_unknown_names_are_refused_and_no_path_means_no_disk(tmp_path):
    store = SecretStore(None)
    with pytest.raises(ValueError):
        store.set("PATH", "x")
    store.set("MCP_TOKEN", "t" * 30)
    assert store.get("MCP_TOKEN") == "t" * 30


# ---- state follows the store -----------------------------------------------------------------------------------
def test_adding_and_removing_a_key_updates_modes_and_provider(tmp_path):
    store = SecretStore(None)
    state = AiState(EventBus(), AI, tmp_path / "ai.json", store)
    assert state.configured() == [] and state.set_mode(True) is True
    store.set("OPENAI_API_KEY", OTHER)
    state.keys_changed()
    assert state.configured() == ["openai"] and state.provider == "openai"
    assert state.set_mode(True)
    store.clear("OPENAI_API_KEY")
    state.keys_changed()
    assert state.mode is True  # AI mode stays on; the chat reports the missing key when asked


def test_model_options_override_settings_and_persist(tmp_path):
    state = AiState(EventBus(), AI, tmp_path / "ai.json", SecretStore(None))
    assert state.model("openai") == "m-o" and state.stt_model() == "t-default" and state.cloud_stt() is True and state.input == "vocal"
    state.set_options(models={"openai": "gpt-x"}, stt_model="whisper-z")
    state.set_mode(True, "text")
    again = AiState(EventBus(), AI, tmp_path / "ai.json", SecretStore(None))
    assert (again.model("openai"), again.stt_model(), again.cloud_stt(), again.input) == ("gpt-x", "whisper-z", False, "text")
    again.set_options(models={"openai": ""})  # empty = back to settings.json
    assert again.model("openai") == "m-o"


def test_provider_client_is_rebuilt_when_key_or_model_changes(tmp_path):
    store = SecretStore(None)
    store.set("ANTHROPIC_API_KEY", KEY)
    state = AiState(EventBus(), AI, tmp_path / "ai.json", store)
    built = []
    ai = AiAssistant(EventBus(), state, lambda: [], provider_factory=lambda name: built.append(state.fingerprint(name)) or object())
    ai._provider("anthropic")
    ai._provider("anthropic")
    assert len(built) == 1
    state.set_options(models={"anthropic": "m-new"})
    ai._provider("anthropic")
    store.set("ANTHROPIC_API_KEY", "sk-ant-" + "z" * 30)
    ai._provider("anthropic")
    assert [m for _, m in built] == ["m-a", "m-new", "m-new"] and built[2][0].endswith("z" * 30)


# ---- HTTP -------------------------------------------------------------------------------------------------------
@pytest.fixture
def app(tmp_path):
    bus = EventBus()
    store = SecretStore(tmp_path / "s.json")
    state = AiState(bus, AI, tmp_path / "ai.json", store)
    mcp = start_mcp(0, "", lambda: [])
    server = start_ui(bus, 0, host="127.0.0.1", ai_state=state, mcp=mcp)
    yield f"http://127.0.0.1:{server.server_address[1]}", state, store, mcp
    for s in (server, mcp):
        s.shutdown()
        s.server_close()


def post(base, path, body, origin=None):
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    request = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            return reply.status, reply.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def test_saving_a_key_enables_the_provider_and_is_never_echoed(app):
    base, state, store, _ = app
    status, body = post(base, "/settings/key", {"name": "anthropic", "value": KEY})
    assert status == 200 and state.configured() == ["anthropic"] and store.get("ANTHROPIC_API_KEY") == KEY
    assert KEY not in body and json.loads(body)["providers"][1]["key_source"] == "ui"
    with urllib.request.urlopen(base + "/settings", timeout=5) as reply:
        assert KEY not in reply.read().decode()
    with urllib.request.urlopen(base + "/ai", timeout=5) as reply:
        assert KEY not in reply.read().decode()


def test_removing_the_key_returns_to_env_default(app, monkeypatch):
    base, state, _, _ = app
    monkeypatch.setenv("ANTHROPIC_API_KEY", "default-from-env-key-0000")
    post(base, "/settings/key", {"name": "anthropic", "value": KEY})
    status, body = post(base, "/settings/key", {"name": "anthropic", "value": ""})
    assert status == 200 and json.loads(body)["providers"][1]["key_source"] == "env"
    assert state.store.get("ANTHROPIC_API_KEY") == "default-from-env-key-0000"


@pytest.mark.parametrize("value", ["short", "has space " + "a" * 30, "a" * 15 + "\n" + "a" * 15, "é" * 30])
def test_things_that_are_not_keys_are_rejected(app, value):
    base, state, _, _ = app
    assert post(base, "/settings/key", {"name": "openai", "value": value})[0] == 400
    assert state.configured() == []
    assert post(base, "/settings/key", {"name": "bard", "value": KEY})[0] == 400


def test_surrounding_whitespace_from_a_paste_is_trimmed(app):
    base, state, store, _ = app
    assert post(base, "/settings/key", {"name": "openai", "value": "  " + OTHER + "\n"})[0] == 200
    assert store.get("OPENAI_API_KEY") == OTHER


def test_cross_site_posts_are_refused(app):
    base, state, _, _ = app
    host = base.split("//")[1]
    assert post(base, "/settings/key", {"name": "openai", "value": OTHER}, origin="https://evil.example")[0] == 403
    assert state.configured() == []
    assert post(base, "/ai/mode?on=1", {}, origin="https://evil.example")[0] == 403
    assert post(base, "/settings/key", {"name": "openai", "value": OTHER}, origin="http://" + host)[0] == 200  # our own page


def test_models_are_saved_and_validated_and_the_input_choice_is_not_a_setting(app):
    base, state, _, _ = app
    status, body = post(base, "/settings/ai", {"models": {"openai": "gpt-9"}, "stt_model": "tx-1", "cloud_stt": False})
    assert status == 200 and state.model("openai") == "gpt-9" and state.stt_model() == "tx-1"
    assert state.input == "vocal" and json.loads(body)["input"] == "vocal"  # the old cloud_stt key is ignored: the header selector decides
    assert post(base, "/settings/ai", {"models": {"openai": "bad model!"}})[0] == 400


def test_mcp_token_is_generated_once_and_works_at_once(app):
    base, state, store, mcp = app
    assert json.loads(urllib.request.urlopen(base + "/settings").read())["mcp"]["token_source"] == "none"
    status, body = post(base, "/settings/mcp", {"action": "generate"})
    token = json.loads(body)["new_token"]
    assert status == 200 and len(token) == 48 and mcp.token == token and store.get("MCP_TOKEN") == token
    with urllib.request.urlopen(base + "/settings") as reply:
        assert token not in reply.read().decode()  # never shown again
    ping = urllib.request.Request(f"http://127.0.0.1:{mcp.server_address[1]}/mcp/{token}", data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}', method="POST")
    assert urllib.request.urlopen(ping, timeout=5).status == 200
    post(base, "/settings/mcp", {"action": "clear"})
    assert mcp.token == ""
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(urllib.request.Request(ping.full_url, data=ping.data, method="POST"), timeout=5)
    assert error.value.code == 404
