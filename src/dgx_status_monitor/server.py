"""Read-only HTTP access to the latest cached sample."""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Tuple

from .sampler import SnapshotCache


class MonitorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], cache: SnapshotCache, stale_ms: float):
        self.cache = cache
        self.stale_ms = stale_ms
        super().__init__(address, MonitorHandler)


class MonitorHandler(BaseHTTPRequestHandler):
    server: MonitorHTTPServer

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5.0)

    def log_message(self, format: str, *args: object) -> None:
        # Monitoring clients can poll frequently; do not fill the journal.
        pass

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _respond(self, status: int, body: bytes = b"", allow: bool = False) -> None:
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        if allow:
            self.send_header("Allow", "GET, HEAD, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    @staticmethod
    def _json(value: object) -> bytes:
        return json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        sample = self.server.cache.get()
        if path == "/api/v1/snapshot":
            if sample is None:
                self._respond(503, b'{"error":"warming_up"}')
            else:
                self._respond(200, sample.json_bytes)
        elif path == "/healthz":
            age_ms = max(0.0, (time.monotonic() - sample.sampled_at) * 1000) if sample else None
            healthy = age_ms is not None and age_ms <= self.server.stale_ms
            body = self._json({
                "status": "ok" if healthy else "stale",
                "age_ms": round(age_ms) if age_ms is not None else None,
                "sample_duration_ms": round(sample.duration_ms, 1) if sample else None,
            })
            self._respond(200 if healthy else 503, body)
        else:
            self._respond(404, b'{"error":"not_found"}')

    do_HEAD = do_GET

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _method_not_allowed(self) -> None:
        self._respond(405, b'{"error":"method_not_allowed"}', allow=True)

    def __getattr__(self, name: str):
        # BaseHTTPRequestHandler normally sends 501 for unknown HTTP methods.
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)
