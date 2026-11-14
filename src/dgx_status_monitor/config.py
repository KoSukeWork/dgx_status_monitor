"""Command-line and environment configuration."""

import argparse
import logging
import math
import os
from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    interval_ms: float
    stale_ms: float
    log_level: str


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def _positive_ms(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def parse_args(argv: Optional[Sequence[str]] = None) -> Config:
    parser = argparse.ArgumentParser(description="DGX GB10 status monitor")
    parser.add_argument("--host", default=os.getenv("DGX_MONITOR_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=_port, default=os.getenv("DGX_MONITOR_PORT", "9500"))
    parser.add_argument(
        "--interval-ms", type=_positive_ms, default=os.getenv("DGX_MONITOR_INTERVAL_MS", "500")
    )
    parser.add_argument(
        "--stale-ms", type=_positive_ms, default=os.getenv("DGX_MONITOR_STALE_MS", "2000")
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default=os.getenv("DGX_MONITOR_LOG_LEVEL", "INFO"),
    )
    args = parser.parse_args(argv)
    if not args.host.strip():
        parser.error("host must not be empty")
    return Config(args.host, args.port, args.interval_ms, args.stale_ms, args.log_level)


def configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level), format="%(asctime)s %(levelname)s %(message)s")
