import json
import logging
from types import SimpleNamespace

import pytest

from dgx_status_monitor.collector import (
    MIB,
    Collector,
    NVMLCollector,
    cpu_percent,
    memory_fields,
    parse_cpu_stat,
    parse_meminfo,
)


def process(pid, size, name="private-process"):
    return SimpleNamespace(pid=pid, usedGpuMemory=size, name=name)


def test_cpu_deltas_and_guest_are_not_counted_twice():
    previous = parse_cpu_stat("cpu 100 10 20 200 10 5 5 0 90 80\ncpu0 1 2 3 4")
    current = parse_cpu_stat("cpu 120 10 30 220 20 5 5 0 999 999")
    assert previous == (350, 210)
    assert current == (410, 240)
    assert cpu_percent(previous, current) == 50.0
    assert cpu_percent(current, current) is None
    assert cpu_percent(current, previous) is None


def test_memory_values_and_independent_fields():
    info = parse_meminfo("MemTotal: 8192000 kB\nMemAvailable: 2048000 kB\nSwapTotal: 1024000 kB\nSwapFree: 256000 kB\n")
    assert memory_fields(info) == {
        "total_mib": 8000, "available_mib": 2000, "used_mib": 6000,
        "percent": 75.0, "swap_total_mib": 1000, "swap_used_mib": 750,
    }
    partial = memory_fields(parse_meminfo("MemTotal: 4096 kB\nMemAvailable: bad kB\nSwapTotal: 0 kB\n"))
    assert partial == {
        "total_mib": 4, "available_mib": None, "used_mib": None,
        "percent": None, "swap_total_mib": 0, "swap_used_mib": None,
    }
    assert memory_fields({"MemTotal": 0, "MemAvailable": 0})["percent"] is None


def test_proc_root_injection_and_first_snapshot_has_cpu_value(tmp_path, fake_nvml):
    (tmp_path / "stat").write_text("cpu 100 0 0 100 0 0 0 0 88 0\n")
    (tmp_path / "loadavg").write_text("3.1 2.8 2.5 1/20 42\n")
    (tmp_path / "meminfo").write_text("MemTotal: 4096 kB\nMemAvailable: 1024 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    collector = Collector(tmp_path, NVMLCollector(fake_nvml))
    collector.prime_cpu()
    (tmp_path / "stat").write_text("cpu 110 0 0 110 0 0 0 0 999 0\n")
    snapshot = collector.collect()
    assert snapshot["cpu"] == {"percent": 50.0, "load_avg": [3.1, 2.8, 2.5]}
    assert snapshot["memory"]["used_mib"] == 3
    assert snapshot["memory"]["percent"] == 75.0


def test_missing_proc_and_nvml_return_null_without_losing_keys(tmp_path, fake_nvml):
    fake_nvml.init_error = OSError("no NVML")
    collector = Collector(tmp_path / "does-not-exist", NVMLCollector(fake_nvml))
    collector.prime_cpu()
    snapshot = collector.collect()
    assert snapshot["cpu"] == {"percent": None, "load_avg": None}
    assert len(snapshot["memory"]) == 7
    assert all(value is None for value in snapshot["memory"].values())
    assert set(snapshot["gpu"]) == {"util_percent", "sm_clock_mhz", "power_w", "temp_c"}
    assert all(value is None for value in snapshot["gpu"].values())


def test_gpu_memory_union_deduplicates_skips_unavailable_and_hides_processes(tmp_path, fake_nvml):
    fake_nvml.values["compute"] = [
        process(123456789, 3 * MIB), process(987654321, 7 * MIB),
        process(333, None), process(444, 0xFFFFFFFFFFFFFFFF), process(555, 0xFFFFFFFF),
    ]
    fake_nvml.values["graphics"] = [process(123456789, 5 * MIB), process(666, 2 * MIB)]
    snapshot = Collector(tmp_path, NVMLCollector(fake_nvml)).collect()
    assert snapshot["memory"]["gpu_used_mib"] == 14
    payload = json.dumps(snapshot)
    assert "123456789" not in payload and "987654321" not in payload
    assert "private-process" not in payload and '"pid"' not in payload


@pytest.mark.parametrize("invalid", [None, 0xFFFFFFFFFFFFFFFF, 0xFFFFFFFF, -1])
def test_gpu_memory_with_processes_but_no_available_values_is_null(fake_nvml, invalid):
    fake_nvml.values["compute"] = [process(1, invalid)]
    fake_nvml.values["graphics"] = [process(2, invalid)]
    memory, gpu = NVMLCollector(fake_nvml).sample()
    assert memory is None
    assert gpu["util_percent"] == 73


def test_gpu_memory_empty_process_lists_are_zero(fake_nvml):
    assert NVMLCollector(fake_nvml).sample()[0] == 0


def test_gpu_memory_partial_values_warn_only_when_entering_partial_state(fake_nvml, caplog):
    nvml = NVMLCollector(fake_nvml)
    fake_nvml.values["compute"] = [process(123456789, 3 * MIB), process(987654321, None)]
    with caplog.at_level(logging.WARNING):
        assert nvml.sample()[0] == 3
        assert nvml.sample()[0] == 3
        assert len(caplog.records) == 1
        fake_nvml.values["compute"] = [process(123456789, 3 * MIB)]
        assert nvml.sample()[0] == 3
        fake_nvml.values["compute"].append(process(987654321, None))
        assert nvml.sample()[0] == 3
        assert len(caplog.records) == 2
        fake_nvml.values["compute"] = [process(987654321, None)]
        assert nvml.sample()[0] is None
        fake_nvml.values["compute"].append(process(123456789, 3 * MIB))
        assert nvml.sample()[0] == 3
        assert len(caplog.records) == 3
    assert all("may be too low" in record.message for record in caplog.records)
    assert all("123456789" not in record.message and "987654321" not in record.message for record in caplog.records)


def test_gpu_memory_zero_is_valid_and_duplicate_unavailable_value_warns(fake_nvml, caplog):
    fake_nvml.values["compute"] = [process(1, None)]
    fake_nvml.values["graphics"] = [process(1, 0)]
    with caplog.at_level(logging.WARNING):
        assert NVMLCollector(fake_nvml).sample()[0] == 0
    assert len(caplog.records) == 1 and "may be too low" in caplog.records[0].message


def test_gpu_memory_partial_and_total_failure(fake_nvml):
    fake_nvml.values["compute"] = RuntimeError("unsupported")
    fake_nvml.values["graphics"] = [process(1, 2 * MIB)]
    nvml = NVMLCollector(fake_nvml)
    assert nvml.sample()[0] == 2
    fake_nvml.values["graphics"] = RuntimeError("unsupported")
    memory, gpu = nvml.sample()
    assert memory is None
    assert gpu == {"util_percent": 73, "sm_clock_mhz": 2398, "power_w": 10.4, "temp_c": 43}


def test_gpu_independent_fields_and_temperature_v_fallback(fake_nvml):
    fake_nvml.values["util"] = RuntimeError("not supported")
    fake_nvml.values["power"] = RuntimeError("not supported")
    fake_nvml.values["temp"] = RuntimeError("old function unavailable")
    memory, gpu = NVMLCollector(fake_nvml).sample()
    assert memory == 0
    assert gpu == {"util_percent": None, "sm_clock_mhz": 2398, "power_w": None, "temp_c": 44}


def test_nvml_connection_loss_invalidates_all_gpu_fields_and_retries_once_per_five_seconds(fake_nvml, caplog):
    now = [10.0]
    nvml = NVMLCollector(fake_nvml, clock=lambda: now[0])
    fake_nvml.values["compute"] = [process(1, MIB)]
    with caplog.at_level(logging.INFO):
        assert nvml.sample()[0] == 1
        fake_nvml.values["power"] = fake_nvml.NVMLError_GpuIsLost("lost")
        memory, gpu = nvml.sample()
        assert memory is None and all(value is None for value in gpu.values())
        assert fake_nvml.shutdown_calls == 1
        now[0] = 14.99
        nvml.sample()
        assert fake_nvml.init_calls == 1
        fake_nvml.values["power"] = 10000
        now[0] = 15.0
        assert nvml.sample()[0] == 1
        assert fake_nvml.init_calls == 2
        nvml.close()
        assert fake_nvml.shutdown_calls == 2
    assert sum("NVML unavailable" in record.message for record in caplog.records) == 1
    assert sum("NVML available" in record.message for record in caplog.records) == 2


def test_failed_initialization_is_rate_limited_and_logs_only_state_changes(fake_nvml, caplog):
    now = [0.0]
    fake_nvml.init_error = OSError("driver missing")
    nvml = NVMLCollector(fake_nvml, clock=lambda: now[0])
    with caplog.at_level(logging.WARNING):
        for timestamp in (0.0, 0.5, 4.9, 5.0, 5.5, 10.0):
            now[0] = timestamp
            assert nvml.sample()[0] is None
    assert fake_nvml.init_calls == 3
    assert sum("NVML unavailable" in record.message for record in caplog.records) == 1
