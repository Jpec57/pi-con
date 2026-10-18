from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from http.client import HTTPResponse

import websocket

logger = logging.getLogger(__name__)

TIMEOUT = 15  # seconds; Home Assistant needs ~10 s to open a camera stream the first time
HLS_PREFIX = "/api/hls/"  # the only Home Assistant path the UI may reach through us


# Entity domains worth showing as "devices"; the rest of Home Assistant's states (sun, zone, tts, update...) is plumbing.
DEVICE_DOMAINS = {
    "camera", "light", "switch", "climate", "cover", "fan", "lock", "media_player", "vacuum", "sensor", "binary_sensor", "button",
}


NOISY_PREFIXES = ("sensor.sun_", "sensor.backup_")  # Home Assistant's own housekeeping sensors, not devices

class HomeAssistantError(Exception):
    pass


def device_list(states: list[dict], cameras: dict[str, str]) -> list[dict]:
    """Shape Home Assistant entity states for the UI: name, domain, state and, for configured cameras, the voice name."""
    camera_by_entity = {entity: name for name, entity in cameras.items()}
    devices = []
    for state in states:
        entity_id = state["entity_id"]
        domain = entity_id.split(".", 1)[0]
        if domain not in DEVICE_DOMAINS or entity_id.startswith(NOISY_PREFIXES):
            continue
        devices.append({
            "id": entity_id,
            "name": state.get("attributes", {}).get("friendly_name") or entity_id,
            "domain": domain,
            "state": state.get("state", ""),
            "camera": camera_by_entity.get(entity_id),
        })
    return sorted(devices, key=lambda device: (device["domain"] != "camera", device["domain"], device["name"].casefold()))


class HomeAssistant:
    """The little of Home Assistant that pi-con uses: ask for a camera's live HLS stream, then relay it.

    The long-lived access token stays on the Pi: the browser only talks to pi-con, which proxies the HLS files.
    """

    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.token = token

    def camera_stream_path(self, entity: str) -> str:
        """Path (on Home Assistant) of the master HLS playlist of a camera entity."""
        if not self.token:
            raise HomeAssistantError("HA_TOKEN is not set")
        ws_url = "ws" + self.url[len("http"):] + "/api/websocket"
        try:
            ws = websocket.create_connection(ws_url, timeout=TIMEOUT)
        except (OSError, websocket.WebSocketException) as error:
            raise HomeAssistantError(f"Home Assistant is unreachable: {error}") from error
        try:
            ws.recv()  # auth_required
            ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            if json.loads(ws.recv()).get("type") != "auth_ok":
                raise HomeAssistantError("Home Assistant rejected HA_TOKEN")
            ws.send(json.dumps({"id": 1, "type": "camera/stream", "entity_id": entity, "format": "hls"}))
            reply = json.loads(ws.recv())
        except (OSError, websocket.WebSocketException, ValueError) as error:
            raise HomeAssistantError(f"Home Assistant stream request failed: {error}") from error
        finally:
            ws.close()
        if not reply.get("success"):
            raise HomeAssistantError(f"{entity}: {reply.get('error', {}).get('message', 'no stream')}")
        return reply["result"]["url"]

    def states(self) -> list[dict]:
        """All entity states, as Home Assistant's REST API returns them."""
        if not self.token:
            raise HomeAssistantError("HA_TOKEN is not set")
        request = urllib.request.Request(self.url + "/api/states", headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:  # noqa: S310 (fixed http origin)
                return json.load(reply)
        except urllib.error.HTTPError as error:
            raise HomeAssistantError(f"Home Assistant answered {error.code}") from error
        except (OSError, ValueError) as error:
            raise HomeAssistantError(f"Home Assistant is unreachable: {error}") from error

    def call_service(self, service: str, data: dict) -> None:
        """Call a Home Assistant service, e.g. call_service('light.turn_on', {'entity_id': 'light.salon'})."""
        if not self.token:
            raise HomeAssistantError("HA_TOKEN is not set")
        domain, name = service.split(".", 1)
        request = urllib.request.Request(
            f"{self.url}/api/services/{domain}/{name}", data=json.dumps(data).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(request, timeout=TIMEOUT).close()  # noqa: S310 (fixed http origin)
        except urllib.error.HTTPError as error:
            raise HomeAssistantError(f"{service}: Home Assistant answered {error.code}") from error
        except OSError as error:
            raise HomeAssistantError(f"Home Assistant is unreachable: {error}") from error

    def open_hls(self, path_and_query: str) -> HTTPResponse:
        """Open one HLS file (playlist, init or segment) on Home Assistant. Refuses anything that is not an HLS path."""
        if not path_and_query.startswith(HLS_PREFIX) or ".." in path_and_query:
            raise HomeAssistantError("not an HLS path")
        try:
            return urllib.request.urlopen(self.url + path_and_query, timeout=TIMEOUT)  # noqa: S310 (fixed http origin)
        except urllib.error.HTTPError as error:
            raise HomeAssistantError(f"Home Assistant answered {error.code}") from error
        except OSError as error:
            raise HomeAssistantError(f"Home Assistant is unreachable: {error}") from error
