"""Repeatable failover drill: healthy -> one down -> recovery -> total outage -> load."""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from .config import (
    BackendConfig,
    GatewayConfig,
    HealthConfig,
    ListenConfig,
    LoggingConfig,
    MetricsConfig,
    RequestConfig,
)
from .demo_backend import DemoBackendServer
from .gateway import GatewayServer
from .log import configure_logging


@dataclass
class DrillPhase:
    name: str
    ok: bool
    detail: str
    duration_ms: float

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
        }


@dataclass
class DrillReport:
    phases: list[DrillPhase]
    requests: int
    concurrency: int
    load_rps: float
    load_p95_ms: float
    ok: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "requests": self.requests,
            "concurrency": self.concurrency,
            "load_rps": round(self.load_rps, 2),
            "load_p95_ms": round(self.load_p95_ms, 2),
            "phases": [phase.to_dict() for phase in self.phases],
        }


class _Response:
    def __init__(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.status = status
        self.body = body
        self.headers = headers

    @property
    def backend(self) -> str | None:
        return self.headers.get("x-backend")


def _request(port: int, path: str, timeout: float = 3.0) -> _Response:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"X-Request-Id": "drill"})
        response = connection.getresponse()
        body = response.read()
        return _Response(
            int(response.status),
            body,
            {key.lower(): value for key, value in response.getheaders()},
        )
    finally:
        connection.close()


def _wait(predicate: Callable[[], bool], timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _backend_state(gateway: GatewayServer, name: str) -> bool:
    response = _request(gateway.port, "/admin/backends")
    payload = json.loads(response.body)
    for backend in payload["backends"]:
        if backend["name"] == name:
            return bool(backend["healthy"])
    return False


def _phase(
    name: str, fn: Callable[[], tuple[bool, str]]
) -> DrillPhase:
    started = time.perf_counter()
    try:
        ok, detail = fn()
    except Exception as exc:  # keep the drill going long enough to report
        ok, detail = False, f"exception: {exc}"
    return DrillPhase(
        name=name,
        ok=ok,
        detail=detail,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )


def run_drill(requests: int = 300, concurrency: int = 32) -> DrillReport:
    if requests < 1:
        raise ValueError("requests must be at least 1")
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")

    backends = [
        DemoBackendServer(name, "127.0.0.1", 0).start()
        for name in ("alpha", "bravo", "charlie")
    ]
    config = GatewayConfig(
        backends=tuple(
            BackendConfig(name=backend.state.name, url=backend.url)
            for backend in backends
        ),
        listen=ListenConfig("127.0.0.1", 0),
        request=RequestConfig(
            timeout_seconds=0.5,
            max_attempts=3,
            retry_on_status=(502, 503, 504),
        ),
        health=HealthConfig(
            path="/healthz",
            interval_seconds=0.05,
            timeout_seconds=0.2,
            failure_threshold=2,
            success_threshold=2,
            initial_state="healthy",
        ),
        metrics=MetricsConfig(enabled=True, path="/metrics"),
        logging=LoggingConfig(level="ERROR", format="text"),
    )
    gateway = GatewayServer(config)
    phases: list[DrillPhase] = []
    load_rps = 0.0
    load_p95 = 0.0
    try:
        gateway.start_background()
        if not gateway.wait_ready(timeout=3):
            raise RuntimeError("gateway never became ready")

        def baseline() -> tuple[bool, str]:
            names: Counter[str] = Counter()
            for _ in range(30):
                response = _request(gateway.port, "/")
                if response.status != 200:
                    return False, f"unexpected status {response.status}"
                names[response.backend or "?"] += 1
            ok = len(names) >= 2
            return ok, f"distributed across {len(names)} backends: {dict(names)}"

        phases.append(_phase("baseline routing", baseline))

        def one_down() -> tuple[bool, str]:
            backends[0].fail()
            if not _wait(lambda: not _backend_state(gateway, "alpha")):
                return False, "alpha was not marked unhealthy before timeout"
            names: Counter[str] = Counter()
            for _ in range(30):
                response = _request(gateway.port, "/")
                if response.status != 200:
                    return False, f"unexpected status {response.status}"
                names[response.backend or "?"] += 1
            ok = "alpha" not in names and len(names) >= 2
            return ok, f"traffic shifted to {dict(names)}"

        phases.append(_phase("one backend unhealthy", one_down))

        def recovery() -> tuple[bool, str]:
            backends[0].recover()
            if not _wait(lambda: _backend_state(gateway, "alpha")):
                return False, "alpha never recovered"
            names: Counter[str] = Counter()
            for _ in range(30):
                response = _request(gateway.port, "/")
                names[response.backend or "?"] += 1
            ok = "alpha" in names
            return ok, f"traffic returned to {dict(names)}"

        phases.append(_phase("recovery", recovery))

        def total_outage() -> tuple[bool, str]:
            for backend in backends:
                backend.fail()
            if not _wait(lambda: not gateway.app.ready()):
                return False, "gateway stayed ready during total outage"
            statuses = [_request(gateway.port, "/").status for _ in range(10)]
            readyz = _request(gateway.port, "/readyz").status
            healthz = _request(gateway.port, "/healthz").status
            ok = all(status == 503 for status in statuses)
            ok = ok and readyz == 503 and healthz == 200
            detail = (
                f"proxy={sorted(set(statuses))}, readyz={readyz}, "
                f"healthz={healthz}"
            )
            for backend in backends:
                backend.recover()
            if not _wait(lambda: gateway.app.ready()):
                return False, f"{detail}; gateway did not recover"
            return ok, detail

        phases.append(_phase("total outage", total_outage))

        def load() -> tuple[bool, str]:
            nonlocal load_rps, load_p95
            statuses: list[int] = []
            durations: list[float] = []
            lock = threading.Lock()

            def hit(_: int) -> None:
                started = time.perf_counter()
                response = _request(gateway.port, "/", timeout=5.0)
                duration = time.perf_counter() - started
                with lock:
                    statuses.append(response.status)
                    durations.append(duration)

            wall_started = time.perf_counter()
            with ThreadPoolExecutor(
                max_workers=min(concurrency, requests)
            ) as pool:
                list(pool.map(hit, range(requests)))
            wall = time.perf_counter() - wall_started
            load_rps = len(statuses) / wall if wall else 0.0
            sorted_durations = sorted(durations)
            index = max(0, int(round(0.95 * (len(sorted_durations) - 1))))
            load_p95 = (
                sorted_durations[index] * 1000 if sorted_durations else 0.0
            )
            failures = sum(1 for status in statuses if status != 200)
            ok = failures == 0 and len(statuses) == requests
            return ok, (
                f"{len(statuses)} requests, {failures} failures, "
                f"{load_rps:.1f} req/s, p95 {load_p95:.1f} ms"
            )

        phases.append(_phase("load / recovery", load))
    finally:
        gateway.shutdown()
        for backend in backends:
            backend.stop()

    return DrillReport(
        phases=phases,
        requests=requests,
        concurrency=concurrency,
        load_rps=load_rps,
        load_p95_ms=load_p95,
        ok=all(phase.ok for phase in phases),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ha-gateway-drill",
        description="Repeatable failover and load drill for the HA gateway.",
    )
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    configure_logging("ERROR", "text")
    try:
        report = run_drill(args.requests, args.concurrency)
    except Exception as exc:
        print(f"drill failed to start: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        width = max(len(phase.name) for phase in report.phases)
        print("HA gateway failover drill")
        print(f"{'PHASE'.ljust(width)}  RESULT  DETAIL")
        for phase in report.phases:
            print(
                f"{phase.name.ljust(width)}  "
                f"{'PASS' if phase.ok else 'FAIL':<6}  {phase.detail}"
            )
        print()
        print(
            f"Overall: {'PASS' if report.ok else 'FAIL'} "
            f"({report.requests} requests, concurrency {report.concurrency}, "
            f"{report.load_rps:.1f} req/s, p95 {report.load_p95_ms:.1f} ms)"
        )
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
