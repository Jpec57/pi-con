from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .ha import HomeAssistantError
from .tools import Tool, build_tools  # noqa: F401 (build_tools re-exported for callers)

logger = logging.getLogger(__name__)

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 64 * 1024
RATE_LIMIT = 120  # requests per minute, whole server: the endpoint may be reachable from the internet
INSTRUCTIONS = (
    "Controls Jarvis, a voice assistant on a Raspberry Pi at home. Tools act on the Pi's own screen and devices; "
    "they never return camera video or images."
)


class McpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], token: str, tools: Callable[[], list[Tool]]) -> None:
        super().__init__(address, _Handler)
        self.token = ""
        self.tools = tools
        self._hits: list[float] = []
        self._lock = threading.Lock()

    def set_token(self, token: str) -> None:
        """Change the secret at runtime ('' switches the endpoint off: every request is a 404)."""
        if token and len(token) < 24:
            raise ValueError("MCP_TOKEN must be at least 24 characters (generate one in the settings or with: openssl rand -hex 24)")
        self.token = token

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            self._hits = [t for t in self._hits if now - t < 60]
            if len(self._hits) >= RATE_LIMIT:
                return False
            self._hits.append(now)
            return True


class _Handler(BaseHTTPRequestHandler):
    server: McpServer

    def _send(self, status: int, body: bytes = b"", content_type: str = "application/json") -> None:
        self.send_response(status)
        if body:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        """The secret is the second path segment: /mcp/<token>. Wrong or missing looks like any other 404."""
        parts = self.path.split("?", 1)[0].strip("/").split("/")
        return bool(self.server.token) and len(parts) == 2 and parts[0] == "mcp" and hmac.compare_digest(parts[1].encode(), self.server.token.encode())

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        if not self._authorised():
            self._send(404)
            return
        if not self.server.allow():
            self._send(429)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            self._send(413 if length > MAX_BODY else 400)
            return
        try:
            message = json.loads(self.rfile.read(length))
        except ValueError:
            self._send(400, json.dumps(_error(None, -32700, "Parse error")).encode())
            return
        replies = [reply for item in (message if isinstance(message, list) else [message]) if (reply := self._dispatch(item)) is not None]
        if not replies:
            self._send(202)  # only notifications
        else:
            self._send(200, json.dumps(replies if isinstance(message, list) else replies[0], ensure_ascii=False).encode())

    def do_GET(self) -> None:  # noqa: N802
        self._send(405 if self._authorised() else 404)  # no server-initiated stream: this server is stateless

    do_DELETE = do_GET  # noqa: N815

    def _dispatch(self, item: object) -> dict | None:
        if not isinstance(item, dict) or item.get("jsonrpc") != "2.0" or "method" not in item:
            return _error(item.get("id") if isinstance(item, dict) else None, -32600, "Invalid request")
        request_id, method, params = item.get("id"), item["method"], item.get("params") or {}
        if "id" not in item:
            return None  # notification, e.g. notifications/initialized
        if method == "initialize":
            wanted = params.get("protocolVersion")
            version = wanted if wanted in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
            return _result(request_id, {
                "protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "pi-con", "version": "0.1.0"}, "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return _result(request_id, {})
        tools = {tool.name: tool for tool in self.server.tools()}
        if method == "tools/list":
            return _result(request_id, {"tools": [
                {"name": t.name, "description": t.description, "inputSchema": t.schema,
                 "annotations": {"readOnlyHint": t.read_only, "destructiveHint": False, "openWorldHint": False}}
                for t in tools.values()
            ]})
        if method == "tools/call":
            tool = tools.get(params.get("name"))
            if tool is None:
                return _error(request_id, -32602, f"Unknown tool: {params.get('name')}")
            logger.info("MCP tool call: %s", tool.name)
            try:
                text, failed = tool.handler(params.get("arguments") or {}), False
            except (ValueError, HomeAssistantError) as error:
                text, failed = str(error), True
            except Exception:
                logger.exception("MCP tool %s crashed", tool.name)
                text, failed = "internal error", True
            return _result(request_id, {"content": [{"type": "text", "text": text}], "isError": failed})
        return _error(request_id, -32601, f"Method not found: {method}")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # the request path contains the secret


def _result(request_id: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def start_mcp(port: int, token: str, tools: Callable[[], list[Tool]], host: str = "127.0.0.1") -> McpServer:
    """Serve MCP on loopback only; a tunnel (cloudflared) is the only thing that should reach it from outside."""
    server = McpServer((host, port), "", tools)
    try:
        server.set_token(token)
    except ValueError as error:  # a bad .env value must not stop the assistant: stay idle until a good token is set
        logger.error("%s; the MCP endpoint stays off", error)
        token = ""
    threading.Thread(target=server.serve_forever, name="mcp", daemon=True).start()
    logger.info("MCP server on %s:%d (loopback; %s)", host, server.server_address[1], "token set" if token else "idle until a token is set in the settings")
    return server
