#!/usr/bin/env python3
"""Calculate release-B Compose resource values from release-A observations."""

from __future__ import annotations

import argparse
import math


MIB = 1024 * 1024


def ceil_mib(value: int) -> int:
    """Round a byte value up to whole MiB."""
    return max(1, math.ceil(value / MIB))


def main() -> None:
    """Print validated Compose resource settings without changing deployment files."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-rss-p99", type=int, required=True, help="API steady-state RSS p99 in bytes")
    parser.add_argument("--api-rss-p95", type=int, required=True, help="API steady-state RSS p95 in bytes")
    parser.add_argument("--browser-rss-peak", type=int, required=True, help="Browser batch peak RSS in bytes")
    parser.add_argument("--browser-pids-peak", type=int, required=True, help="Maximum PID count during browser batches")
    args = parser.parse_args()
    if min(args.api_rss_p99, args.api_rss_p95, args.browser_rss_peak, args.browser_pids_peak) <= 0:
        parser.error("all observations must be positive")

    memory_limit = ceil_mib(math.ceil(max(args.api_rss_p99, args.browser_rss_peak) * 1.30))
    memory_reservation = ceil_mib(args.api_rss_p95)
    pids_limit = max(128, math.ceil(args.browser_pids_peak * 1.50))
    print(f"MEMORY_LIMIT={memory_limit}m")
    print(f"MEMORY_RESERVATION={memory_reservation}m")
    print(f"PIDS_LIMIT={pids_limit}")
    print("CPU_SHARES=1024")


if __name__ == "__main__":
    main()
