"""Command-line entry point."""

import logging
import signal
import threading
from typing import Optional, Sequence

from .collector import Collector, NVMLCollector
from .config import configure_logging, parse_args
from .sampler import Sampler, SnapshotCache
from .server import MonitorHTTPServer

LOG = logging.getLogger(__name__)


def main(argv: Optional[Sequence[str]] = None) -> None:
    config = parse_args(argv)
    configure_logging(config.log_level)
    nvml = NVMLCollector()
    collector = Collector(nvml=nvml)
    cache = SnapshotCache()
    sampler = Sampler(collector, cache, config.interval_ms)
    server = None
    server_thread = None
    stopping = threading.Event()
    original_handlers = {}
    try:
        nvml.ensure_initialized()
        server = MonitorHTTPServer((config.host, config.port), cache, config.stale_ms)
        for signum in (signal.SIGINT, signal.SIGTERM):
            original_handlers[signum] = signal.signal(signum, lambda _sig, _frame: stopping.set())
        sampler.start()
        server_thread = threading.Thread(target=server.serve_forever, name="dgx-http", daemon=True)
        server_thread.start()
        host, port = server.server_address[:2]
        LOG.info(
            "Listening on %s:%s; interval=%gms; NVML=%s; GPU=%s",
            host, port, config.interval_ms, "available" if nvml.available else "unavailable",
            nvml.gpu_name or "unknown",
        )
        while server_thread.is_alive() and not stopping.wait(1.0):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            if server_thread is not None and server_thread.is_alive():
                server.shutdown()
                server_thread.join()
            server.server_close()
        sampler.stop()
        nvml.close()
        for signum, handler in original_handlers.items():
            signal.signal(signum, handler)
        LOG.info("Monitor stopped")


if __name__ == "__main__":
    main()
