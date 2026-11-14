import http.client
import json
import threading
import time

import pytest

from dgx_status_monitor.collector import Collector, NVMLCollector
from dgx_status_monitor.sampler import CachedSample, SnapshotCache
from dgx_status_monitor.server import MonitorHTTPServer


@pytest.fixture
def service():
    cache = SnapshotCache()
    server = MonitorHTTPServer(("127.0.0.1", 0), cache, 2000)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, cache
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def request(server, method, path):
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        client.request(method, path)
        response = client.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        client.close()


def test_snapshot_and_query_string_are_cached_and_complete(service, tmp_path, fake_nvml):
    server, cache = service
    snapshot = Collector(tmp_path, NVMLCollector(fake_nvml)).collect()
    payload = json.dumps(snapshot).encode()
    cache.publish(CachedSample(payload, time.monotonic(), 1.8))
    status, headers, body = request(server, "GET", "/api/v1/snapshot?x=1")
    assert status == 200 and body == payload
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["Access-Control-Allow-Origin"] == "*" and headers["Cache-Control"] == "no-store"
    data = json.loads(body)
    assert set(data) == {"ts", "cpu", "memory", "gpu"}
    assert set(data["cpu"]) == {"percent", "load_avg"}
    assert set(data["memory"]) == {"total_mib", "used_mib", "available_mib", "percent", "swap_total_mib", "swap_used_mib", "gpu_used_mib"}
    assert set(data["gpu"]) == {"util_percent", "sm_clock_mhz", "power_w", "temp_c"}


def test_warming_and_health_ok_stale(service):
    server, cache = service
    status, headers, body = request(server, "GET", "/api/v1/snapshot")
    assert status == 503 and json.loads(body) == {"error": "warming_up"}
    assert headers["Access-Control-Allow-Origin"] == "*" and headers["Cache-Control"] == "no-store"
    status, _, body = request(server, "GET", "/healthz")
    assert status == 503 and json.loads(body) == {"status": "stale", "age_ms": None, "sample_duration_ms": None}
    cache.publish(CachedSample(b"{}", time.monotonic(), 1.83))
    status, _, body = request(server, "GET", "/healthz")
    assert status == 200 and json.loads(body)["status"] == "ok"
    assert json.loads(body)["sample_duration_ms"] == 1.8
    cache.publish(CachedSample(b"{}", time.monotonic() - 3, 2))
    status, _, body = request(server, "GET", "/healthz")
    assert status == 503 and json.loads(body)["status"] == "stale"
    assert json.loads(body)["age_ms"] >= 3000


@pytest.mark.parametrize("method,path,expected", [
    ("HEAD", "/api/v1/snapshot", 200), ("HEAD", "/healthz", 200),
    ("HEAD", "/missing", 404), ("OPTIONS", "/any", 204),
    ("POST", "/api/v1/snapshot", 405), ("PUT", "/api/v1/snapshot", 405),
    ("DELETE", "/api/v1/snapshot", 405), ("PATCH", "/api/v1/snapshot", 405),
    ("BREW", "/api/v1/snapshot", 405), ("GET", "/missing?x=1", 404),
])
def test_methods_and_cors(service, method, path, expected):
    server, cache = service
    cache.publish(CachedSample(b'{"sample":true}', time.monotonic(), 1))
    status, headers, body = request(server, method, path)
    assert status == expected
    assert headers["Access-Control-Allow-Origin"] == "*" and headers["Cache-Control"] == "no-store"
    if method in {"HEAD", "OPTIONS"}:
        assert body == b""
    if method == "HEAD" and path == "/api/v1/snapshot":
        assert int(headers["Content-Length"]) == len(b'{"sample":true}')
    if expected == 405 or method == "OPTIONS":
        assert headers["Allow"] == "GET, HEAD, OPTIONS"
