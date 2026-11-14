from types import SimpleNamespace

import pytest


class FakeNVML:
    NVML_CLOCK_SM = 1
    NVML_TEMPERATURE_GPU = 0
    NVML_VALUE_NOT_AVAILABLE_ulonglong = 0xFFFFFFFFFFFFFFFF

    class NVMLError_GpuIsLost(Exception):
        pass

    class NVMLError_Uninitialized(Exception):
        pass

    def __init__(self):
        self.values = {
            "compute": [],
            "graphics": [],
            "util": SimpleNamespace(gpu=73),
            "clock": 2398,
            "power": 10404,
            "temp": 43,
            "temp_v": 44,
        }
        self.init_calls = 0
        self.shutdown_calls = 0
        self.init_error = None
        self.handle = object()

    def _get(self, key):
        value = self.values[key]
        if isinstance(value, Exception):
            raise value
        return value

    def nvmlInit(self):
        self.init_calls += 1
        if self.init_error:
            raise self.init_error

    def nvmlShutdown(self):
        self.shutdown_calls += 1

    def nvmlDeviceGetHandleByIndex(self, index):
        assert index == 0
        return self.handle

    def nvmlDeviceGetName(self, handle):
        assert handle is self.handle
        return b"Fake GB10"

    def nvmlDeviceGetComputeRunningProcesses(self, handle):
        return self._get("compute")

    def nvmlDeviceGetGraphicsRunningProcesses(self, handle):
        return self._get("graphics")

    def nvmlDeviceGetUtilizationRates(self, handle):
        return self._get("util")

    def nvmlDeviceGetClockInfo(self, handle, clock):
        assert clock == self.NVML_CLOCK_SM
        return self._get("clock")

    def nvmlDeviceGetPowerUsage(self, handle):
        return self._get("power")

    def nvmlDeviceGetTemperature(self, handle, sensor):
        assert sensor == self.NVML_TEMPERATURE_GPU
        return self._get("temp")

    def nvmlDeviceGetTemperatureV(self, handle, sensor):
        assert sensor == self.NVML_TEMPERATURE_GPU
        return self._get("temp_v")


@pytest.fixture
def fake_nvml():
    return FakeNVML()
