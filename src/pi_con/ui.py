from __future__ import annotations

import json
import logging
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files

from .events import EventBus

logger = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 15


class UiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], bus: EventBus) -> None:
        super().__init__(address, _Handler)
        self.bus = bus
        self.page = files("pi_con").joinpath("static/index.html").read_bytes()


class _Handler(BaseHTTPRequestHandler):
    server: UiServer

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, "text/html; charset=utf-8", self.server.page)
        elif path == "/healthz":
            self._send(200, "text/plain; charset=utf-8", b"ok")
        elif path == "/events":
            self._stream_events()
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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


def start_ui(bus: EventBus, port: int, host: str = "0.0.0.0") -> UiServer:
    """Serve the UI on a background thread. Bound to all interfaces so other devices on the LAN can open it."""
    server = UiServer((host, port), bus)
    threading.Thread(target=server.serve_forever, name="ui", daemon=True).start()
    logger.info("UI on http://localhost:%d", server.server_address[1])
    return server
