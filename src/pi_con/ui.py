from __future__ import annotations

import json
import logging
import queue
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections.abc import Callable
from importlib.resources import files
from urllib.parse import parse_qs, unquote, urlsplit

from .events import EventBus
from .ai.state import KEY_ENV
from .ha import HLS_PREFIX, HomeAssistant, HomeAssistantError, device_list

logger = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 15
KEY_PATTERN = re.compile(r"[A-Za-z0-9_\-.]{20,400}")  # what API keys look like; also rejects pasted whitespace or newlines
MODEL_PATTERN = re.compile(r"[A-Za-z0-9_\-.:/]{0,80}")


class UiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        bus: EventBus,
        on_listen: Callable[[], bool] | None = None,
        actions: dict[str, Callable[[], bool]] | None = None,
        debug_info: Callable[[], dict] | None = None,
        wake_phrase: str = "the wake word",
        ha: HomeAssistant | None = None,
        cameras: dict[str, str] | None = None,
        ai_state=None,
        mcp=None,
        speaker=None,
        recordings=None,
        on_record: Callable[[float], bool] | None = None,
    ) -> None:
        super().__init__(address, _Handler)
        self.recordings = recordings  # recordings.RecordingStore or None
        self.on_record = on_record
        self.speaker = speaker  # tts.Speaker or None
        self.ai_state = ai_state
        self.mcp = mcp  # McpServer or None
        self.ha = ha
        self.cameras = cameras or {}  # camera name -> Home Assistant entity id
        self.bus = bus
        self.on_listen = on_listen
        self.actions = actions or {}
        self.debug_info = debug_info
        page = files("pi_con").joinpath("static/index.html").read_text(encoding="utf-8")
        literal = json.dumps(wake_phrase).replace("<", "\\u003c")  # a JS string literal that cannot close the <script>
        self.boot = str(int(time.time() * 1000))  # changes at every start: an open page compares it and reloads itself (a deploy ships new JavaScript)
        self.page = page.replace('"{{WAKE_PHRASE}}"', literal).replace('"{{BOOT_ID}}"', json.dumps(self.boot)).encode()


class _Handler(BaseHTTPRequestHandler):
    server: UiServer

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, "text/html; charset=utf-8", self.server.page)
        elif path == "/boot":
            self._send(200, "text/plain; charset=utf-8", self.server.boot.encode())
        elif path == "/healthz":
            self._send(200, "text/plain; charset=utf-8", b"ok")
        elif path == "/debug":
            body = json.dumps(self.server.debug_info() if self.server.debug_info else {}).encode()
            self._send(200, "application/json", body)
        elif path == "/events":
            self._stream_events()
        elif path == "/settings":
            self._send(200, "application/json", json.dumps(self._settings_view()).encode())
        elif path == "/ai":
            state = self.server.ai_state
            self._send(200, "application/json", json.dumps(state.snapshot() if state else {"mode": False, "providers": []}).encode())
        elif path == "/recordings":
            store = self.server.recordings
            self._send(200, "application/json", json.dumps(store.list() if store else []).encode())
        elif path.startswith("/recordings/") and path.endswith(".wav"):
            store = self.server.recordings
            wav = store.wav(path[len("/recordings/"):-len(".wav")]) if store else None
            if wav is None:
                self._send(404, "text/plain; charset=utf-8", b"not found")
            else:
                self._send(200, "audio/wav", wav.read_bytes())
        elif path == "/tts":
            speaker = self.server.speaker
            self._send(200, "application/json", json.dumps(speaker.snapshot() if speaker else {"enabled": False, "voice": ""}).encode())
        elif path == "/devices":
            self._devices()
        elif path == "/static/hls.min.js":
            self._send(200, "text/javascript; charset=utf-8", files("pi_con").joinpath("static/hls.min.js").read_bytes())
        elif path.startswith("/camera/") and path.endswith("/stream"):
            self._camera_stream(unquote(path[len("/camera/"):-len("/stream")]))
        elif path.startswith("/ha" + HLS_PREFIX):
            self._relay_hls(self.path[len("/ha"):])
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def _settings_view(self) -> dict:
        state, mcp = self.server.ai_state, self.server.mcp
        view = state.settings_view() if state else {"providers": []}
        store = state.store if state else None
        view["mcp"] = {
            "available": mcp is not None, "port": mcp.server_address[1] if mcp else None,
            "token_source": store.source("MCP_TOKEN") if store else "none",
        }
        return view  # no secret in here, ever: only where each one comes from

    def _json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if 0 < length <= 8192 else None
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def _same_origin(self) -> bool:
        """Refuse browser requests that come from another site (a web page cannot change settings through your browser)."""
        origin = self.headers.get("Origin")
        return origin is None or urlsplit(origin).netloc == self.headers.get("Host")

    def _settings_post(self, route: str) -> None:
        state, body = self.server.ai_state, self._json_body()
        if state is None or body is None:
            self._send(400, "text/plain; charset=utf-8", b"bad request")
        elif route == "key":
            name, value = body.get("name"), str(body.get("value") or "").strip()
            if name not in KEY_ENV:
                self._send(400, "text/plain; charset=utf-8", b"unknown provider")
            elif value and not KEY_PATTERN.fullmatch(value):
                self._send(400, "text/plain; charset=utf-8", b"That does not look like an API key (20+ letters, digits, - _ . only, no spaces)")
            else:
                (state.store.set if value else lambda n, _v: state.store.clear(n))(KEY_ENV[name], value)  # empty = back to the .env default
                state.keys_changed()
                state.announce()
                self._send(200, "application/json", json.dumps(self._settings_view()).encode())
        elif route == "ai":
            models = body.get("models") if isinstance(body.get("models"), dict) else None
            stt_model = body.get("stt_model")
            checks = [*(models or {}).values(), *([stt_model] if stt_model is not None else [])]
            if not all(isinstance(v, str) and MODEL_PATTERN.fullmatch(v.strip()) for v in checks):
                self._send(400, "text/plain; charset=utf-8", b"invalid model name")
                return
            state.set_options(
                {k: v.strip() for k, v in (models or {}).items()} if models is not None else None,
                stt_model.strip() if stt_model is not None else None,
            )
            state.announce()
            self._send(200, "application/json", json.dumps(self._settings_view()).encode())
        elif route == "mcp":
            mcp = self.server.mcp
            if mcp is None:
                self._send(409, "text/plain; charset=utf-8", b"The MCP server is not enabled (set mcp_port in config/settings.json)")
            elif body.get("action") == "generate":
                token = secrets.token_hex(24)
                state.store.set("MCP_TOKEN", token)
                mcp.set_token(token)
                self._send(200, "application/json", json.dumps({**self._settings_view(), "new_token": token}).encode())  # shown once
            elif body.get("action") == "clear":
                state.store.clear("MCP_TOKEN")
                mcp.set_token(state.store.get("MCP_TOKEN"))  # the .env default, or off
                self._send(200, "application/json", json.dumps(self._settings_view()).encode())
            else:
                self._send(400, "text/plain; charset=utf-8", b"unknown action")
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def _ai_post(self, route: str, query: dict[str, list[str]]) -> None:
        state = self.server.ai_state
        if state is None:
            self._send(404, "text/plain; charset=utf-8", b"AI mode is not available")
        elif route == "mode":
            on = query.get("on", ["0"])[0] == "1"
            choice = query.get("input", [""])[0]
            if choice not in ("", "text", "vocal"):
                self._send(400, "text/plain; charset=utf-8", b"input must be text or vocal")
            elif state.set_mode(on, choice or None):
                self._send(200, "application/json", json.dumps(state.snapshot()).encode())
            else:
                self._send(409, "text/plain; charset=utf-8", b"Cannot change the AI mode")
        elif route == "provider":
            if state.set_provider(query.get("name", [""])[0]):
                self._send(200, "application/json", json.dumps(state.snapshot()).encode())
            else:
                self._send(409, "text/plain; charset=utf-8", b"That provider has no API key configured")
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def _tts_post(self, route: str, query: dict[str, list[str]]) -> None:
        speaker = self.server.speaker
        if speaker is None:
            self._send(404, "text/plain; charset=utf-8", b"Spoken replies are not available")
        elif route == "mode":
            speaker.set_enabled(query.get("on", ["0"])[0] == "1")
            self._send(200, "application/json", json.dumps(speaker.snapshot()).encode())
        elif route == "test":
            speaker.say("Bonjour, je suis Jarvis. Je peux maintenant te répondre à voix haute.", force=True)
            self._send(202, "text/plain; charset=utf-8", b"ok")
        elif route == "stop":
            speaker.stop()
            self._send(202, "text/plain; charset=utf-8", b"ok")
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        parts = urlsplit(self.path)
        if not self._same_origin():
            self._send(403, "text/plain; charset=utf-8", b"cross-site request refused")
        elif parts.path == "/recordings/test":
            seconds = float(parse_qs(parts.query).get("seconds", ["6"])[0] or 6)
            started = self.server.on_record is not None and self.server.on_record(seconds)
            self._send(202 if started else 409, "text/plain; charset=utf-8", b"recording" if started else b"recordings are off")
        elif parts.path.startswith("/tts/"):
            self._tts_post(parts.path[len("/tts/"):], parse_qs(parts.query))
        elif parts.path.startswith("/settings/"):
            self._settings_post(parts.path[len("/settings/"):])
        elif parts.path.startswith("/ai/"):
            self._ai_post(parts.path[len("/ai/"):], parse_qs(parts.query))
        elif self.path.split("?", 1)[0] == "/listen":
            started = self.server.on_listen is not None and self.server.on_listen()
            self._send(202 if started else 409, "text/plain; charset=utf-8", b"listening" if started else b"speech-to-text is off")
        elif (action := self.server.actions.get(self.path.split("?", 1)[0].lstrip("/"))) is not None:
            self._send(202, "text/plain; charset=utf-8", b"ok" if action() else b"refused")
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def _send(self, status: int, content_type: str, body: bytes, encoding: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _devices(self) -> None:
        """Everything Home Assistant knows about, for the debug view. A Home Assistant outage is reported, not fatal."""
        devices, error = [], None
        try:
            devices = device_list(self.server.ha.states(), self.server.cameras) if self.server.ha else []
        except HomeAssistantError as failure:
            error = str(failure)
        self._send(200, "application/json", json.dumps({"devices": devices, "error": error}).encode())

    def _camera_stream(self, name: str) -> None:
        entity = self.server.cameras.get(name)
        if entity is None or self.server.ha is None:
            self._send(404, "text/plain; charset=utf-8", b"unknown camera")
            return
        try:
            hls_path = self.server.ha.camera_stream_path(entity)
        except HomeAssistantError as error:
            logger.warning("Camera %s: %s", name, error)
            self._send(502, "text/plain; charset=utf-8", str(error).encode())
            return
        self._send(200, "application/json", json.dumps({"url": "/ha" + hls_path}).encode())

    def _relay_hls(self, path_and_query: str) -> None:
        """Pass one HLS file from Home Assistant to the browser (same origin for hls.js, token never leaves the Pi)."""
        try:
            with self.server.ha.open_hls(path_and_query) as upstream:  # type: ignore[union-attr]
                content_type = upstream.headers.get("Content-Type", "application/octet-stream")
                encoding = upstream.headers.get("Content-Encoding")  # Home Assistant gzips playlists; relay the bytes as they are
                body = upstream.read()
        except (HomeAssistantError, AttributeError) as error:
            self._send(502, "text/plain; charset=utf-8", str(error).encode())
            return
        try:
            self._send(200, content_type, body, encoding)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the viewer closed the camera

    def _stream_events(self) -> None:
        """Server-Sent Events; the browser reconnects on its own and sends Last-Event-ID."""
        try:
            after_id = int(self.headers.get("Last-Event-ID") or 0)
        except ValueError:
            after_id = 0
        subscriber, backlog = self.server.bus.subscribe(after_id)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            for event in backlog:
                self._write_event(event)
            while True:
                try:
                    event = subscriber.get(timeout=KEEPALIVE_SECONDS)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self._write_event(event)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser tab was closed
        finally:
            self.server.bus.unsubscribe(subscriber)

    def _write_event(self, event: dict) -> None:
        self.wfile.write(f"id: {event['id']}\ndata: {json.dumps(event)}\n\n".encode())
        self.wfile.flush()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug("ui: " + format, *args)


def start_ui(
    bus: EventBus,
    port: int,
    host: str = "0.0.0.0",
    on_listen: Callable[[], bool] | None = None,
    actions: dict[str, Callable[[], bool]] | None = None,
    debug_info: Callable[[], dict] | None = None,
    wake_phrase: str = "the wake word",
    ha: HomeAssistant | None = None,
    cameras: dict[str, str] | None = None,
    ai_state=None,
    mcp=None,
    speaker=None,
    recordings=None,
    on_record: Callable[[float], bool] | None = None,
) -> UiServer:
    """Serve the UI on a background thread. Bound to all interfaces so other devices on the LAN can open it."""
    server = UiServer((host, port), bus, on_listen, actions, debug_info, wake_phrase, ha, cameras, ai_state, mcp, speaker, recordings, on_record)
    threading.Thread(target=server.serve_forever, name="ui", daemon=True).start()
    logger.info("UI on http://localhost:%d", server.server_address[1])
    return server
