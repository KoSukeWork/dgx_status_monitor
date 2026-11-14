import json
import signal
import threading

import pytest

from dgx_status_monitor import sampler as sampler_module
from dgx_status_monitor.collector import NVMLCollector
from dgx_status_monitor.config import parse_args
from dgx_status_monitor.sampler import Sampler, SnapshotCache


def test_sampler_recovers_from_errors_and_skips_missed_beats(monkeypatch, caplog):
    now = [0.0]
    starts = []

    class Collector:
        def prime_cpu(self):
            self.primed = True

        def collect(self):
            starts.append(now[0])
            if len(starts) == 1:
                raise RuntimeError("once")
            if len(starts) == 2:
                now[0] += 0.25
            return {"count": len(starts)}

    class Event:
        def wait(self, duration):
            if len(starts) >= 3:
                return True
            now[0] += duration
            return False

    monkeypatch.setattr(sampler_module.time, "monotonic", lambda: now[0])
    collector = Collector()
    cache = SnapshotCache()
    thread = Sampler(collector, cache, 100)
    thread._stopping = Event()
    thread.run()
    assert collector.primed
    assert starts == pytest.approx([0.1, 0.2, 0.5])
    assert json.loads(cache.get().json_bytes) == {"count": 3}
    assert "Sampling failed; continuing" in caplog.text


def test_nvml_retries_at_five_seconds_even_with_a_long_sample_interval(monkeypatch, fake_nvml):
    now = [0.0]
    attempts = []
    calls = []
    original_init = fake_nvml.nvmlInit

    def init():
        attempts.append(now[0])
        original_init()

    fake_nvml.nvmlInit = init
    fake_nvml.init_error = OSError("driver missing")
    nvml = NVMLCollector(fake_nvml, clock=lambda: now[0])
    assert not nvml.ensure_initialized()

    class Collector:
        def __init__(self):
            self.nvml = nvml

        def prime_cpu(self):
            pass

        def collect(self):
            calls.append(now[0])
            return {"gpu": self.nvml.sample()[1]}

    class Event:
        def wait(self, duration):
            if calls:
                return True
            now[0] += duration
            if now[0] >= 5:
                fake_nvml.init_error = None
            return False

    monkeypatch.setattr(sampler_module.time, "monotonic", lambda: now[0])
    thread = Sampler(Collector(), SnapshotCache(), 12000)
    thread._stopping = Event()
    thread.run()
    assert attempts == [0.0, 5.0]
    assert calls == [12.0]


def test_config_defaults_environment_and_cli_priority(monkeypatch):
    for name in ("HOST", "PORT", "INTERVAL_MS", "STALE_MS", "LOG_LEVEL"):
        monkeypatch.delenv("DGX_MONITOR_" + name, raising=False)
    config = parse_args([])
    assert (config.host, config.port, config.interval_ms, config.stale_ms, config.log_level) == ("0.0.0.0", 9500, 500, 2000, "INFO")
    monkeypatch.setenv("DGX_MONITOR_PORT", "1234")
    monkeypatch.setenv("DGX_MONITOR_INTERVAL_MS", "250")
    monkeypatch.setenv("DGX_MONITOR_LOG_LEVEL", "debug")
    config = parse_args(["--port", "4321", "--log-level", "error"])
    assert (config.port, config.interval_ms, config.log_level) == (4321, 250, "ERROR")


@pytest.mark.parametrize("arguments", [["--port", "65536"], ["--interval-ms", "0"], ["--stale-ms", "nan"], ["--log-level", "NOPE"], ["--host", " "]])
def test_config_rejects_invalid_values(arguments):
    with pytest.raises(SystemExit):
        parse_args(arguments)


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signal_shuts_down_server_and_nvml(monkeypatch, signum):
    from dgx_status_monitor import __main__ as entrypoint

    ready = threading.Event()

    class NVML:
        available = False
        gpu_name = None
        closed = False

        def ensure_initialized(self):
            pass

        def sample(self):
            ready.set()
            return None, dict.fromkeys(("util_percent", "sm_clock_mhz", "power_w", "temp_c"))

        def close(self):
            self.closed = True

    nvml = NVML()
    monkeypatch.setattr(entrypoint, "NVMLCollector", lambda: nvml)

    def trigger_signal():
        if ready.wait(5):
            signal.raise_signal(signum)

    trigger = threading.Thread(target=trigger_signal, daemon=True)
    trigger.start()
    entrypoint.main(["--host", "127.0.0.1", "--port", "0", "--interval-ms", "10"])
    trigger.join(timeout=5)
    assert ready.is_set() and nvml.closed
