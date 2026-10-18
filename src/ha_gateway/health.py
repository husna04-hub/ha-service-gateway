"""Active health checks and the backend health state machine."""

from __future__ import annotations

import http.client
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event, Thread
from typing import Callable

from .config import BackendConfig, HealthConfig

USER_AGENT = "ha-gateway-health/0.1"


@dataclass
class HealthState:
    failure_threshold: int
    success_threshold: int
    state: str = "healthy"
    consecutive_successes: int = 0
    consecutive_failures: int = 0
    checks: int = 0
    failures: int = 0
    last_error: str | None = None
    last_checked: str | None = None

    def record(self, ok: bool, error: str | None = None) -> str | None:
        """Record a probe; return 'healthy'/'unhealthy' only on transition."""
        self.checks += 1
        self.last_checked = _now()
        if ok:
            self.consecutive_successes += 1
            self.consecutive_failures = 0
            self.last_error = None
            if (
                self.state == "unhealthy"
                and self.consecutive_successes >= self.success_threshold
            ):
                self.state = "healthy"
                return "healthy"
            return None

        self.consecutive_failures += 1
        self.consecutive_successes = 0
        self.failures += 1
        self.last_error = error
        if (
            self.state == "healthy"
            and self.consecutive_failures >= self.failure_threshold
        ):
            self.state = "unhealthy"
            return "unhealthy"
        return None

    @property
    def healthy(self) -> bool:
        return self.state == "healthy"

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "healthy": self.healthy,
            "consecutive_successes": self.consecutive_successes,
            "consecutive_failures": self.consecutive_failures,
            "checks": self.checks,
            "failures": self.failures,
            "last_error": self.last_error,
            "last_checked": self.last_checked,
        }


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str
    latency_ms: float
    status_code: int | None = None


def health_path(backend: BackendConfig, health: HealthConfig) -> str:
    if backend.base_path:
        return f"{backend.base_path}{health.path}"
    return health.path


def probe_backend(
    backend: BackendConfig, health: HealthConfig
) -> ProbeResult:
    started = time.perf_counter()
    path = health_path(backend, health)
    connection_class = (
        http.client.HTTPSConnection
        if backend.scheme == "https"
        else http.client.HTTPConnection
    )
    connection: http.client.HTTPConnection | None = None
    try:
        connection = connection_class(
            backend.host, backend.port, timeout=health.timeout_seconds
        )
        connection.request(
            "GET",
            path,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        status = int(response.status)
        response.read(1024)
        ok = 200 <= status < 400
        return ProbeResult(
            ok=ok,
            detail=f"HTTP {status}",
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            status_code=status,
        )
    except (TimeoutError, socket.timeout):
        return _failure(
            f"timeout after {health.timeout_seconds:g}s", started
        )
    except socket.gaierror as exc:
        return _failure(f"DNS resolution failed: {exc}", started)
    except ConnectionRefusedError:
        return _failure(
            f"connection refused on {backend.host}:{backend.port}", started
        )
    except ssl.SSLError as exc:
        return _failure(f"TLS error: {exc}", started)
    except (http.client.HTTPException, OSError) as exc:
        return _failure(f"connection error: {exc}", started)
    finally:
        if connection is not None:
            connection.close()


class HealthProber:
    """One daemon thread per backend, with an interruptible interval."""

    def __init__(
        self,
        backend: BackendConfig,
        config: HealthConfig,
        callback: Callable[[str, bool, str], None],
        stop_event: Event,
    ) -> None:
        self.backend = backend
        self.config = config
        self.callback = callback
        self.stop_event = stop_event
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = Thread(
            target=self._run,
            name=f"health-{self.backend.name}",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            result = probe_backend(self.backend, self.config)
            if self.stop_event.is_set():
                break
            self.callback(self.backend.name, result.ok, result.detail)
            self.stop_event.wait(self.config.interval_seconds)

    def stop(self, timeout: float = 2.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def join(self, timeout: float = 2.0) -> None:
        self.stop(timeout)


def _failure(detail: str, started: float) -> ProbeResult:
    return ProbeResult(
        ok=False,
        detail=detail,
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
