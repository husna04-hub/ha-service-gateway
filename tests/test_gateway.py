from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from ha_gateway.config import MetricsConfig, RequestConfig
from ha_gateway.gateway import GatewayServer
from tests.conftest import make_config, request, wait_for_health, wait_until


def _admin(gateway) -> dict:
    response = request(gateway.port, "/admin/backends")
    assert response.status == 200
    return response.json


def test_builtin_endpoints(backend_factory, gateway_factory) -> None:
    backend = backend_factory("api")
    gateway = gateway_factory([backend])
    health = request(gateway.port, "/healthz")
    assert health.status == 200
    assert health.json["status"] == "ok"

    ready = request(gateway.port, "/readyz")
    assert ready.status == 200
    assert ready.json["ready"] is True

    admin = request(gateway.port, "/admin/backends")
    assert admin.status == 200
    assert admin.json["healthy_backends"] == 1

    metrics = request(gateway.port, "/metrics")
    assert metrics.status == 200
    assert "ha_gateway_backend_up" in metrics.body.decode()


def test_routing_reaches_multiple_backends(backend_factory, gateway_factory) -> None:
    backends = [backend_factory(name) for name in ("a", "b", "c")]
    gateway = gateway_factory(backends)
    names = []
    for _ in range(30):
        response = request(gateway.port, "/")
        assert response.status == 200
        names.append(response.backend)
    assert set(names) == {"a", "b", "c"}
    assert all(name is not None for name in names)


def test_failover_and_recovery(backend_factory, gateway_factory) -> None:
    a = backend_factory("a")
    b = backend_factory("b")
    gateway = gateway_factory([a, b])

    assert request(gateway.port, "/").status == 200
    a.fail()
    assert wait_for_health(gateway, "a", False)
    for _ in range(12):
        response = request(gateway.port, "/")
        assert response.status == 200
        assert response.backend == "b"

    a.recover()
    assert wait_for_health(gateway, "a", True)
    names = {request(gateway.port, "/").backend for _ in range(12)}
    assert "a" in names


def test_all_backends_unhealthy_returns_503(
    backend_factory, gateway_factory
) -> None:
    a = backend_factory("a")
    b = backend_factory("b")
    gateway = gateway_factory([a, b])
    a.fail()
    b.fail()
    assert wait_for_health(gateway, "a", False)
    assert wait_for_health(gateway, "b", False)
    assert wait_until(lambda: not gateway.app.ready())

    response = request(gateway.port, "/")
    assert response.status == 503
    assert response.json["error"] == "no_healthy_backends"
    assert response.headers["retry-after"] == "1"
    assert request(gateway.port, "/readyz").status == 503
    assert request(gateway.port, "/healthz").status == 200


def test_timeout_fails_over_to_second_backend(
    backend_factory, gateway_factory
) -> None:
    a = backend_factory("a")
    b = backend_factory("b")
    gateway = gateway_factory(
        [a, b],
        request=RequestConfig(
            timeout_seconds=0.1,
            max_attempts=2,
            retry_on_status=(502, 503, 504),
        ),
    )
    a.set_delay(300)
    response = request(gateway.port, "/", timeout=3.0)
    assert response.status == 200
    assert response.backend == "b"
    assert response.headers["x-gateway-attempts"] == "2"


def test_upstream_5xx_is_retried(backend_factory, gateway_factory) -> None:
    a = backend_factory("a")
    b = backend_factory("b")
    gateway = gateway_factory([a, b])
    a.set_status(502)
    response = request(gateway.port, "/")
    assert response.status == 200
    assert response.backend == "b"
    assert response.headers["x-gateway-attempts"] == "2"


def test_post_body_is_forwarded(backend_factory, gateway_factory) -> None:
    backend = backend_factory("only")
    gateway = gateway_factory([backend])
    response = request(
        gateway.port,
        "/echo",
        method="POST",
        body=b'{"hello":"world"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status == 200
    payload = response.json
    assert payload["method"] == "POST"
    assert payload["body"] == '{"hello":"world"}'


def test_request_id_is_propagated(backend_factory, gateway_factory) -> None:
    backend = backend_factory("only")
    gateway = gateway_factory([backend])
    response = request(
        gateway.port,
        "/",
        headers={"X-Request-Id": "trace-123"},
    )
    assert response.headers["x-request-id"] == "trace-123"


def test_head_request_has_no_body(backend_factory, gateway_factory) -> None:
    backend = backend_factory("only")
    gateway = gateway_factory([backend])
    response = request(gateway.port, "/healthz", method="HEAD")
    assert response.status == 200
    assert response.body == b""
    assert int(response.headers["content-length"]) > 0


def test_metrics_can_be_disabled(backend_factory, gateway_factory) -> None:
    backend = backend_factory("only")
    gateway = gateway_factory(
        [backend], metrics=MetricsConfig(enabled=False, path="/metrics")
    )
    assert request(gateway.port, "/metrics").status == 404


def test_metrics_reflect_requests(backend_factory, gateway_factory) -> None:
    backend = backend_factory("only")
    gateway = gateway_factory([backend])
    request(gateway.port, "/")
    request(gateway.port, "/")
    metrics = request(gateway.port, "/metrics").body.decode()
    assert (
        'ha_gateway_requests_total{method="GET",status="200",backend="only"} 2'
        in metrics
    )
    assert 'ha_gateway_backend_up{backend="only"} 1' in metrics


def test_concurrent_requests(backend_factory, gateway_factory) -> None:
    backends = [backend_factory(name) for name in ("a", "b", "c")]
    gateway = gateway_factory(backends)

    def hit(_: int) -> int:
        return request(gateway.port, "/").status

    with ThreadPoolExecutor(max_workers=24) as pool:
        statuses = list(pool.map(hit, range(120)))
    assert statuses == [200] * 120


def test_health_checks_update_backend_state(
    backend_factory, gateway_factory
) -> None:
    backend = backend_factory("only")
    gateway = gateway_factory([backend])
    assert wait_until(
        lambda: _admin(gateway)["backends"][0]["checks"] >= 1
    )
    status = _admin(gateway)
    assert status["backends"][0]["healthy"] is True
    assert status["backends"][0]["checks"] >= 1

    backend.fail()
    assert wait_for_health(gateway, "only", False)
    status = _admin(gateway)
    assert status["backends"][0]["state"] == "unhealthy"
    assert status["backends"][0]["failures"] >= 1


def test_server_run_loop_stops_cleanly(backend_factory) -> None:
    backend = backend_factory("run-loop")
    server = GatewayServer(make_config([backend]))
    stop = threading.Event()
    thread = threading.Thread(target=server.run, args=(stop,), daemon=True)
    thread.start()

    def probe() -> bool:
        try:
            return request(server.port, "/").status == 200
        except OSError:
            return False

    try:
        assert wait_until(probe, timeout=3.0)
    finally:
        stop.set()
        thread.join(timeout=3.0)
    assert not thread.is_alive()
