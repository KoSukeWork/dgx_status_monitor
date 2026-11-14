"""Periodic sampling and a single immutable, pre-serialized cache entry."""

import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .collector import Collector

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class CachedSample:
    json_bytes: bytes
    sampled_at: float
    duration_ms: float


class SnapshotCache:
    def __init__(self) -> None:
        self._latest: Optional[CachedSample] = None

    def get(self) -> Optional[CachedSample]:
        return self._latest

    def publish(self, sample: CachedSample) -> None:
        # A reader sees one whole immutable entry, including its matching metadata.
        self._latest = sample


class Sampler(threading.Thread):
    def __init__(self, collector: Collector, cache: SnapshotCache, interval_ms: float):
        super().__init__(name="dgx-sampler", daemon=True)
        self.collector = collector
        self.cache = cache
        self.interval = interval_ms / 1000.0
        self._stopping = threading.Event()

    def stop(self) -> None:
        self._stopping.set()
        if self.ident is not None:
            self.join()

    def run(self) -> None:
        try:
            self.collector.prime_cpu()
        except Exception:
            LOG.exception("Could not establish CPU baseline")
        next_sample = time.monotonic() + self.interval
        retry_not_before = 0.0
        while True:
            nvml = getattr(self.collector, "nvml", None)
            retry_at = getattr(nvml, "retry_at", None)
            if retry_at is not None:
                retry_at = max(retry_at, retry_not_before)
            deadline = min(next_sample, retry_at) if retry_at is not None else next_sample
            if self._stopping.wait(max(0.0, deadline - time.monotonic())):
                break
            now = time.monotonic()
            if retry_at is not None and retry_at <= now and now < next_sample:
                # Retrying is independent of a possibly much longer sample interval.
                try:
                    nvml.ensure_initialized()
                    retry_not_before = 0.0
                except Exception:
                    LOG.exception("NVML retry failed; continuing")
                    retry_not_before = time.monotonic() + 5.0
                continue
            if now < next_sample:
                continue
            started = time.monotonic()
            try:
                snapshot = self.collector.collect()
                payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
                duration_ms = (time.monotonic() - started) * 1000
                self.cache.publish(CachedSample(payload, started, duration_ms))
            except Exception:
                LOG.exception("Sampling failed; continuing")
            next_sample += self.interval
            now = time.monotonic()
            if next_sample <= now:
                # Skip missed slots rather than performing catch-up samples.
                next_sample += (math.floor((now - next_sample) / self.interval) + 1) * self.interval
