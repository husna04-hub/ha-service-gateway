"""Small dependency-free load generator for a running gateway.

Usage:
    python scripts/load_test.py --url http://127.0.0.1:8080 \
        --requests 1000 --concurrency 32
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit


def percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(
        len(sorted_values) - 1,
        max(0, math.ceil(fraction * len(sorted_values)) - 1),
    )
    return sorted_values[index]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load test a running HA gateway.")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--path", default="/")
    parser.add_argument("--requests", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    parts = urlsplit(args.url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        print("error: --url must be an http(s) URL", file=sys.stderr)
        return 2
    connection_class = (
        http.client.HTTPSConnection
        if parts.scheme == "https"
        else http.client.HTTPConnection
    )
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = (parts.path.rstrip("/") + args.path) or "/"

    statuses: list[int] = []
    durations: list[float] = []
    lock = threading.Lock()

    def hit(_: int) -> None:
        started = time.perf_counter()
        connection = connection_class(host, port, timeout=args.timeout)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            response.read()
            status = int(response.status)
        except Exception:
            status = 0
        finally:
            connection.close()
        with lock:
            statuses.append(status)
            durations.append(time.perf_counter() - started)

    wall_started = time.perf_counter()
    with ThreadPoolExecutor(
        max_workers=min(args.concurrency, args.requests)
    ) as pool:
        list(pool.map(hit, range(args.requests)))
    wall = time.perf_counter() - wall_started

    sorted_durations = sorted(durations)
    success = sum(1 for status in statuses if 200 <= status < 400)
    payload = {
        "url": args.url,
        "path": path,
        "requests": len(statuses),
        "concurrency": args.concurrency,
        "success": success,
        "failures": len(statuses) - success,
        "throughput_rps": round(len(statuses) / wall, 2) if wall else 0.0,
        "latency_ms": {
            "p50": round(percentile(sorted_durations, 0.50) * 1000, 2),
            "p95": round(percentile(sorted_durations, 0.95) * 1000, 2),
            "p99": round(percentile(sorted_durations, 0.99) * 1000, 2),
            "max": round((sorted_durations[-1] if sorted_durations else 0) * 1000, 2),
        },
        "status_counts": {
            str(status): statuses.count(status) for status in sorted(set(statuses))
        },
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Backend URL: {args.url}{path}")
        print(
            f"Requests: {payload['requests']}  concurrency: {args.concurrency}  "
            f"throughput: {payload['throughput_rps']} req/s"
        )
        print(
            f"Success: {success}/{len(statuses)}  "
            f"statuses: {payload['status_counts']}"
        )
        latency = payload["latency_ms"]
        print(
            f"Latency: p50 {latency['p50']} ms  p95 {latency['p95']} ms  "
            f"p99 {latency['p99']} ms  max {latency['max']} ms"
        )
    return 0 if success == len(statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
