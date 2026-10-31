"""Loopback server for the MacroForge synthetic workspace."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socketserver
import stat
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from state import (
    MAX_STATE_BYTES,
    MacroStateStore,
    StateConflictError,
    StateCorruptError,
    StateValidationError,
)

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".macroforge"
DEFAULT_STATE_PATH = STATE_DIR / "state.json"
MAX_REQUEST = MAX_STATE_BYTES + 128
ASSETS = {"/style.css": "style.css", "/macro.js": "macro.js", "/app.js": "app.js"}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Request contains duplicate object keys.")
        result[key] = value
    return result


def _private_data_dir(path: Path) -> None:
    path = Path(path)
    if path.is_symlink():
        raise OSError("MacroForge data directory cannot be a symbolic link.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("MacroForge data path must be a directory.")
    path.chmod(0o700)


class Handler(SimpleHTTPRequestHandler):
    server_version = "MacroForge/1"
    sys_version = ""

    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *_args):
        pass

    def allowed_host(self) -> bool:
        port = self.server.server_port
        return self.headers.get("Host", "").lower() in (f"127.0.0.1:{port}", f"localhost:{port}")

    def state_allowed(self) -> bool:
        port = self.server.server_port
        origins = (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
        token = self.headers.get("X-MacroForge-Request", "")
        return (
            self.allowed_host()
            and self.headers.get("Origin") in origins
            and secrets.compare_digest(token, self.server.request_token)
        )

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )

    def _send_bytes(self, status: int, data: bytes, content_type: str, *, head: bool = False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._security_headers()
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def do_GET(self):
        if not self.allowed_host():
            self.send_error(403, "Host is not allowed")
            return
        parsed = urlsplit(self.path)
        if parsed.path in ("/", "/index.html"):
            if parsed.query not in ("", "desktop=1"):
                self.send_error(400, "Query parameters are not supported")
                return
            page = (ROOT / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", self.server.request_token)
            if parsed.query == "desktop=1":
                page = page.replace("<body>", '<body class="desktop">', 1)
            self._send_bytes(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.query:
            self.send_error(400, "Query parameters are not supported")
            return
        asset = ASSETS.get(parsed.path)
        if asset is None:
            self.send_error(404, "Asset not found")
            return
        path = ROOT / asset
        self._send_bytes(200, path.read_bytes(), self.guess_type(asset))

    def do_HEAD(self):
        if not self.allowed_host():
            self.send_error(403, "Host is not allowed")
            return
        parsed = urlsplit(self.path)
        if parsed.path in ("/", "/index.html") and parsed.query in ("", "desktop=1"):
            page = (ROOT / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", self.server.request_token)
            self._send_bytes(200, page.encode("utf-8"), "text/html; charset=utf-8", head=True)
            return
        asset = ASSETS.get(parsed.path) if not parsed.query else None
        if asset is None:
            self.send_error(404, "Asset not found")
            return
        path = ROOT / asset
        self._send_bytes(200, path.read_bytes(), self.guess_type(asset), head=True)

    def do_POST(self):
        if not self.state_allowed():
            self.send_json(403, {"error": "Local request check failed."})
            return
        if urlsplit(self.path).query:
            self.send_json(400, {"error": "Query parameters are not supported."})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self.send_json(415, {"error": "Send a JSON request."})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Invalid request size."})
            return
        if size < 1 or size > MAX_REQUEST:
            self.send_json(413, {"error": "Request is too large."})
            return
        raw = self.rfile.read(size)
        if len(raw) != size:
            self.send_json(400, {"error": "Incomplete request body."})
            return
        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(data, dict):
                raise ValueError("Request body must be a JSON object.")
            with self.server.state_lock:
                if self.path == "/api/state/load":
                    if data:
                        raise StateValidationError("Load request must be empty.")
                    snapshot = self.server.state_store.load()
                    return self.send_json(200, {
                        "state": snapshot.state,
                        "persisted": snapshot.persisted,
                        "bootstrap_allowed": snapshot.bootstrap_allowed,
                    })
                if self.path == "/api/state/bootstrap":
                    if set(data) != {"state"}:
                        raise StateValidationError("Bootstrap request has unknown fields.")
                    snapshot = self.server.state_store.bootstrap(data["state"])
                    return self.send_json(200, {"state": snapshot.state, "persisted": True, "bootstrap_allowed": False})
                if self.path == "/api/state/save":
                    if set(data) != {"state", "expected_revision"}:
                        raise StateValidationError("Save request has unknown fields.")
                    snapshot = self.server.state_store.save(data["state"], expected_revision=data["expected_revision"])
                    return self.send_json(200, {"state": snapshot.state, "persisted": True, "bootstrap_allowed": False})
            self.send_json(404, {"error": "Unknown action."})
        except StateConflictError as exc:
            self.send_json(409, {"error": str(exc)})
        except StateCorruptError as exc:
            self.send_json(500, {"error": str(exc), "preserved": True})
        except (StateValidationError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except OSError:
            self.send_json(500, {"error": "State could not be saved safely."})

    def send_json(self, status: int, data: dict):
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, raw, "application/json; charset=utf-8")


def make_server(port: int = 8342, state_path: Path | None = None) -> ThreadingHTTPServer:
    selected_path = Path(state_path) if state_path is not None else DEFAULT_STATE_PATH
    _private_data_dir(selected_path.parent)

    class LocalServer(ThreadingHTTPServer):
        daemon_threads = True

        def server_bind(self):
            socketserver.TCPServer.server_bind(self)
            self.server_name = "127.0.0.1"
            self.server_port = self.socket.getsockname()[1]

    server = LocalServer(("127.0.0.1", port), Handler)
    server.request_token = secrets.token_urlsafe(32)
    server.state_store = MacroStateStore(selected_path)
    server.state_lock = threading.RLock()
    return server


def main():
    parser = argparse.ArgumentParser(description="Run MacroForge's synthetic browser workspace.")
    parser.add_argument("--port", type=int, default=8342)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    server = make_server(args.port)
    print(f"MacroForge is ready at http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
