from __future__ import annotations

import json
import logging
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections.abc import Callable
from importlib.resources import files
from urllib.parse import unquote

from .events import EventBus
from .ha import HLS_PREFIX, HomeAssistant, HomeAssistantError, device_list

logger = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 15


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
    ) -> None:
        super().__init__(address, _Handler)
        self.ha = ha
        self.cameras = cameras or {}  # camera name -> Home Assistant entity id
        self.bus = bus
        self.on_listen = on_listen
        self.actions = actions or {}
        self.debug_info = debug_info
        page = files("pi_con").joinpath("static/index.html").read_text(encoding="utf-8")
        literal = json.dumps(wake_phrase).replace("<", "\\u003c")  # a JS string literal that cannot close the <script>
        self.page = page.replace('"{{WAKE_PHRASE}}"', literal).encode()


class _Handler(BaseHTTPRequestHandler):
    server: UiServer

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, "text/html; charset=utf-8", self.server.page)
        elif path == "/healthz":
            self._send(200, "text/plain; charset=utf-8", b"ok")
        elif path == "/debug":
            body = json.dumps(self.server.debug_info() if self.server.debug_info else {}).encode()
            self._send(200, "application/json", body)
        elif path == "/events":
            self._stream_events()
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

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path.split("?", 1)[0] == "/listen":
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
) -> UiServer:
    """Serve the UI on a background thread. Bound to all interfaces so other devices on the LAN can open it."""
    server = UiServer((host, port), bus, on_listen, actions, debug_info, wake_phrase, ha, cameras)
    threading.Thread(target=server.serve_forever, name="ui", daemon=True).start()
    logger.info("UI on http://localhost:%d", server.server_address[1])
    return server
