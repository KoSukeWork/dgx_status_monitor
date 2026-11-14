"""Collect system data without coupling it to HTTP or storage."""

import importlib
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

LOG = logging.getLogger(__name__)
MIB = 1024 * 1024
GPU_KEYS = ("util_percent", "sm_clock_mhz", "power_w", "temp_c")
_FAILED = object()


def parse_cpu_stat(text: str) -> Tuple[int, int]:
    """Return aggregate total and idle counters; guest time is already included."""
    first = text.splitlines()[0].split()
    if len(first) < 9 or first[0] != "cpu":
        raise ValueError("missing aggregate CPU counters")
    values = [int(value) for value in first[1:9]]
    return sum(values), values[3] + values[4]


def cpu_percent(previous: Tuple[int, int], current: Tuple[int, int]) -> Optional[float]:
    total_delta = current[0] - previous[0]
    if total_delta <= 0:
        return None
    idle_delta = current[1] - previous[1]
    return round((total_delta - idle_delta) * 100 / total_delta, 1)


def parse_meminfo(text: str) -> Dict[str, int]:
    """Read individual kB fields, preserving valid fields if another is malformed."""
    values: Dict[str, int] = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        parts = value.split()
        try:
            if parts and (len(parts) == 1 or parts[1] == "kB"):
                values[key] = int(parts[0])
        except ValueError:
            continue
    return values


def memory_fields(info: Dict[str, int]) -> Dict[str, Optional[float]]:
    total = info.get("MemTotal")
    available = info.get("MemAvailable")
    swap_total = info.get("SwapTotal")
    swap_free = info.get("SwapFree")
    used = total - available if total is not None and available is not None else None
    swap_used = swap_total - swap_free if swap_total is not None and swap_free is not None else None
    return {
        "total_mib": total // 1024 if total is not None else None,
        "used_mib": used // 1024 if used is not None else None,
        "available_mib": available // 1024 if available is not None else None,
        "percent": round(used * 100 / total, 1) if used is not None and total else None,
        "swap_total_mib": swap_total // 1024 if swap_total is not None else None,
        "swap_used_mib": swap_used // 1024 if swap_used is not None else None,
    }


class NVMLCollector:
    """Manage one long-lived NVML connection and retry only after it becomes unavailable."""

    def __init__(self, module: Any = None, clock: Callable[[], float] = time.monotonic):
        self._module = module
        self._clock = clock
        self._handle: Any = None
        self._initialized = False
        self._state: Optional[bool] = None
        self._next_retry = 0.0
        self._gpu_memory_partial = False
        self.gpu_name: Optional[str] = None

    @property
    def available(self) -> bool:
        return self._handle is not None

    @property
    def retry_at(self) -> Optional[float]:
        """Next monotonic retry deadline, even when sampling is less frequent."""
        return None if self.available else self._next_retry

    def _set_state(self, available: bool, reason: str = "") -> None:
        if self._state != available:
            if available:
                LOG.info("NVML available; GPU: %s", self.gpu_name or "unknown")
            else:
                LOG.warning("NVML unavailable%s; retrying every 5s", ": " + reason if reason else "")
            self._state = available

    def _shutdown_library(self) -> None:
        if self._initialized:
            try:
                self._module.nvmlShutdown()
            except Exception:
                pass
            self._initialized = False

    def _unavailable(self, exc: Exception) -> None:
        self._handle = None
        self.gpu_name = None
        self._gpu_memory_partial = False
        self._shutdown_library()
        self._next_retry = self._clock() + 5.0
        self._set_state(False, str(exc))

    def _is_connection_error(self, exc: Exception) -> bool:
        if self._module is None:
            return False
        names = (
            "NVMLError_Uninitialized",
            "NVMLError_DriverNotLoaded",
            "NVMLError_LibraryNotFound",
            "NVMLError_GpuIsLost",
        )
        classes = tuple(
            cls for cls in (getattr(self._module, name, None) for name in names) if isinstance(cls, type)
        )
        codes = (
            getattr(self._module, name, None)
            for name in (
                "NVML_ERROR_UNINITIALIZED", "NVML_ERROR_DRIVER_NOT_LOADED",
                "NVML_ERROR_LIBRARY_NOT_FOUND", "NVML_ERROR_GPU_IS_LOST",
            )
        )
        return isinstance(exc, classes) or getattr(exc, "value", None) in tuple(code for code in codes if code is not None)

    def ensure_initialized(self) -> bool:
        if self.available:
            return True
        if self._clock() < self._next_retry:
            return False
        try:
            if self._module is None:
                self._module = importlib.import_module("pynvml")
            self._module.nvmlInit()
            self._initialized = True
            self._handle = self._module.nvmlDeviceGetHandleByIndex(0)
            try:
                name = self._module.nvmlDeviceGetName(self._handle)
                self.gpu_name = name.decode("utf-8", errors="replace") if isinstance(name, bytes) else str(name)
            except Exception as exc:
                if self._is_connection_error(exc):
                    raise
                self.gpu_name = None
            self._set_state(True)
            return True
        except Exception as exc:
            self._unavailable(exc)
            return False

    def _call(self, name: str, *args: Any) -> Any:
        if not self.available:
            return _FAILED
        try:
            return getattr(self._module, name)(self._handle, *args)
        except Exception as exc:
            if self._is_connection_error(exc):
                self._unavailable(exc)
            return _FAILED

    def _not_available_values(self) -> set:
        result = {-1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF}
        for name in ("NVML_VALUE_NOT_AVAILABLE", "NVML_VALUE_NOT_AVAILABLE_uint", "NVML_VALUE_NOT_AVAILABLE_ulonglong"):
            value = getattr(self._module, name, None)
            value = getattr(value, "value", value)
            if isinstance(value, int):
                result.add(value)
        return result

    def _gpu_memory(self) -> Optional[int]:
        processes: Dict[int, int] = {}
        succeeded = False
        saw_process = False
        missing_value = False
        unavailable = self._not_available_values()
        for method in ("nvmlDeviceGetComputeRunningProcesses", "nvmlDeviceGetGraphicsRunningProcesses"):
            items = self._call(method)
            if items is _FAILED or items is None:
                continue
            succeeded = True
            for process in items:
                saw_process = True
                try:
                    pid, used = process.pid, process.usedGpuMemory
                    if used is None or used in unavailable or not isinstance(used, int) or used < 0:
                        missing_value = True
                        continue
                    processes[pid] = max(processes.get(pid, 0), used)
                except (AttributeError, TypeError):
                    missing_value = True
                    continue
        if not succeeded or not self.available:
            self._gpu_memory_partial = False
            return None
        if saw_process and not processes:
            self._gpu_memory_partial = False
            return None
        partial = missing_value and bool(processes)
        if partial and not self._gpu_memory_partial:
            LOG.warning("Some GPU process memory values are unavailable; the reported total may be too low")
        self._gpu_memory_partial = partial
        return sum(processes.values()) // MIB

    def sample(self) -> Tuple[Optional[int], Dict[str, Optional[float]]]:
        gpu: Dict[str, Optional[float]] = dict.fromkeys(GPU_KEYS)
        if not self.ensure_initialized():
            return None, gpu
        memory = self._gpu_memory()

        # Isolate each field so unsupported operations do not hide other values.
        utilization = self._call("nvmlDeviceGetUtilizationRates")
        if utilization is not _FAILED:
            try:
                gpu["util_percent"] = utilization.gpu
            except AttributeError:
                pass
        clock_kind = getattr(self._module, "NVML_CLOCK_SM", None)
        clock = self._call("nvmlDeviceGetClockInfo", clock_kind)
        if clock is not _FAILED:
            gpu["sm_clock_mhz"] = clock
        power = self._call("nvmlDeviceGetPowerUsage")
        if power is not _FAILED:
            try:
                gpu["power_w"] = round(power / 1000, 2)
            except (TypeError, ValueError):
                pass
        sensor = getattr(self._module, "NVML_TEMPERATURE_GPU", None)
        temperature = self._call("nvmlDeviceGetTemperature", sensor)
        if temperature is _FAILED and self.available:
            temperature = self._call("nvmlDeviceGetTemperatureV", sensor)
        if temperature is not _FAILED:
            gpu["temp_c"] = temperature

        # A connection failure anywhere invalidates the entire GPU sample.
        if not self.available:
            return None, dict.fromkeys(GPU_KEYS)
        return memory, gpu

    def close(self) -> None:
        self._handle = None
        self._shutdown_library()


class Collector:
    def __init__(self, proc_root: Path = Path("/proc"), nvml: Optional[NVMLCollector] = None):
        self.proc_root = Path(proc_root)
        self.nvml = nvml if nvml is not None else NVMLCollector()
        self._previous_cpu: Optional[Tuple[int, int]] = None

    def _read(self, name: str) -> str:
        return (self.proc_root / name).read_text(encoding="utf-8")

    def prime_cpu(self) -> None:
        try:
            self._previous_cpu = parse_cpu_stat(self._read("stat"))
        except (OSError, ValueError, IndexError):
            self._previous_cpu = None

    def _cpu_percent(self) -> Optional[float]:
        try:
            current = parse_cpu_stat(self._read("stat"))
            previous, self._previous_cpu = self._previous_cpu, current
            return cpu_percent(previous, current) if previous is not None else None
        except (OSError, ValueError, IndexError):
            return None

    def _load_average(self) -> Optional[list]:
        try:
            values = self._read("loadavg").split()[:3]
            if len(values) != 3:
                return None
            return [float(value) for value in values]
        except (OSError, ValueError):
            return None

    def collect(self) -> Dict[str, Any]:
        timestamp = round(time.time(), 3)
        cpu = {"percent": self._cpu_percent(), "load_avg": self._load_average()}
        try:
            memory = memory_fields(parse_meminfo(self._read("meminfo")))
        except (OSError, ValueError):
            memory = memory_fields({})
        gpu_memory, gpu = self.nvml.sample()
        memory["gpu_used_mib"] = gpu_memory
        return {"ts": timestamp, "cpu": cpu, "memory": memory, "gpu": gpu}
