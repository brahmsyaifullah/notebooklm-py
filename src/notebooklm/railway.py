"""Railway runtime entrypoint for notebooklm-py.

This module provides a minimal HTTP service so Railway can keep the deployment
alive and perform health checks. It does not expose NotebookLM operations over
HTTP; use Railway shell/cron jobs to run CLI commands.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from notebooklm.paths import get_storage_path


def _auth_source() -> str:
    """Return the active auth source: env, file, or none."""
    if os.environ.get("NOTEBOOKLM_AUTH_JSON", "").strip():
        return "env"
    if get_storage_path().exists():
        return "file"
    return "none"


def _safe_home_dir() -> str:
    """Return NOTEBOOKLM_HOME (or default) without creating directories."""
    if home := os.environ.get("NOTEBOOKLM_HOME"):
        return str(Path(home).expanduser().resolve())
    return str((Path.home() / ".notebooklm").resolve())


def status_payload() -> dict[str, Any]:
    """Build a compact status payload for health/readiness endpoints."""
    return {
        "status": "ok",
        "service": "notebooklm-py",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "note": "Use CLI commands via Railway shell/cron for NotebookLM operations.",
        "auth_source": _auth_source(),
        "paths": {
            "home": _safe_home_dir(),
            "storage_state": str(get_storage_path()),
        },
    }


class _Handler(BaseHTTPRequestHandler):
    """HTTP handler for Railway probes."""

    server_version = "notebooklm-railway/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if self.path not in ("/", "/health", "/ready"):
            self._write_json({"status": "not_found"}, status=404)
            return
        self._write_json(status_payload(), status=200)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        # Keep logs concise in managed runtimes.
        return

    def _write_json(self, payload: dict[str, Any], status: int) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _port() -> int:
    """Resolve HTTP port from PORT env var (Railway standard)."""
    raw = os.environ.get("PORT", "8080").strip()
    try:
        port = int(raw)
    except ValueError:
        return 8080
    return port if 1 <= port <= 65535 else 8080


def main() -> None:
    """Start the Railway health server."""
    port = _port()
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    print(f"[notebooklm-py] Railway runtime listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
