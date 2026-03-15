"""Minimal Railway health server for notebooklm-py deployments."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _auth_source() -> str:
    auth = os.environ.get("NOTEBOOKLM_AUTH_JSON", "").strip()
    if auth:
        return "env"

    home = os.environ.get("NOTEBOOKLM_HOME", "").strip()
    if home:
        storage = Path(home).expanduser().resolve() / "storage_state.json"
    else:
        storage = Path.home() / ".notebooklm" / "storage_state.json"

    return "file" if storage.exists() else "none"


def _storage_path() -> str:
    home = os.environ.get("NOTEBOOKLM_HOME", "").strip()
    if home:
        return str((Path(home).expanduser().resolve() / "storage_state.json"))
    return str((Path.home() / ".notebooklm" / "storage_state.json").resolve())


def _port() -> int:
    raw = os.environ.get("PORT", "8080").strip()
    try:
        port = int(raw)
    except ValueError:
        return 8080
    return port if 1 <= port <= 65535 else 8080


class Handler(BaseHTTPRequestHandler):
    server_version = "notebooklm-railway/1.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path not in ("/", "/health", "/ready"):
            self._write_json({"status": "not_found"}, 404)
            return

        payload = {
            "status": "ok",
            "service": "notebooklm-py",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "auth_source": _auth_source(),
            "paths": {
                "storage_state": _storage_path(),
            },
        }
        self._write_json(payload, 200)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def _write_json(self, payload: dict, status: int) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    port = _port()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[notebooklm-py] listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
