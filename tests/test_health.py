from __future__ import annotations

import threading
import time

from ha_gateway.config import BackendConfig, HealthConfig
from ha_gateway.health import HealthProber, HealthState, health_path, probe_backend


def test_health_state_failure_threshold() -> None:
    state = HealthState(failure_threshold=2, success_threshold=2)
    assert state.healthy is True
    assert state.record(False, "boom") is None
    assert state.healthy is True
    assert state.record(False, "boom again") == "unhealthy"
    assert state.healthy is False
    assert state.record(False, "still down") is None
    assert state.failures == 3
    assert state.last_error == "still down"


def test_health_state_success_threshold() -> None:
    state = HealthState(
        failure_threshold=1,
        success_threshold=2,
        state="unhealthy",
    )
    assert state.record(True) is None
    assert state.healthy is False
    assert state.record(True) == "healthy"
    assert state.healthy is True


def test_success_resets_failure_counter() -> None:
    state = HealthState(failure_threshold=3, success_threshold=1)
    state.record(False, "one")
    state.record(False, "two")
    state.record(True)
    assert state.consecutive_failures == 0
    state.record(False, "three")
    assert state.healthy is True
    state.record(False, "four")
    assert state.healthy is True
    state.record(False, "five")
    assert state.healthy is False


def test_health_path_with_base_path() -> None:
    backend = BackendConfig("api", "http://127.0.0.1:9001/base")
    config = HealthConfig(path="/healthz")
    assert health_path(backend, config) == "/base/healthz"


def test_probe_healthy_backend(backend_factory) -> None:
    backend = backend_factory("healthy")
    result = probe_backend(
        BackendConfig("healthy", backend.url),
        HealthConfig(timeout_seconds=0.5),
    )
    assert result.ok is True
    assert result.status_code == 200


def test_probe_unhealthy_backend(backend_factory) -> None:
    backend = backend_factory("unhealthy")
    backend.fail()
    result = probe_backend(
        BackendConfig("unhealthy", backend.url),
        HealthConfig(timeout_seconds=0.5),
    )
    assert result.ok is False
    assert result.status_code == 503


def test_probe_connection_refused() -> None:
    result = probe_backend(
        BackendConfig("down", "http://127.0.0.1:1"),
        HealthConfig(timeout_seconds=0.2),
    )
    assert result.ok is False
    assert result.detail


def test_health_prober_calls_callback(backend_factory) -> None:
    backend = backend_factory("probe")
    stop = threading.Event()
    results: list[tuple[str, bool, str]] = []
    prober = HealthProber(
        BackendConfig("probe", backend.url),
        HealthConfig(interval_seconds=0.05, timeout_seconds=0.3),
        lambda name, ok, detail: results.append((name, ok, detail)),
        stop,
    )
    prober.start()
    deadline = time.monotonic() + 2
    while not results and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    prober.join()
    assert results
    assert results[0][0] == "probe"
    assert results[0][1] is True
