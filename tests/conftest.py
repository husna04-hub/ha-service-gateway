"""Shared fixtures for gateway unit and integration tests."""

from __future__ import annotations

import http.client
import json
import time
from dataclasses import replace
from typing import Callable, Iterable

import pytest

from ha_gateway.config import (
    BackendConfig,
    GatewayConfig,
    HealthConfig,
    ListenConfig,
    LoggingConfig,
    MetricsConfig,
    RequestConfig,
)
from ha_gateway.demo_backend import DemoBackendServer
from ha_gateway.gateway import GatewayServer


def make_config(
    backends: Iterable[DemoBackendServer],
    **overrides,
) -> GatewayConfig:
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
        logging=LoggingConfig(level="ERROR", format="json"),
    )
    return replace(config, **overrides) if overrides else config


@pytest.fixture
def backend_factory():
    servers: list[DemoBackendServer] = []

    def start(name: str = "backend") -> DemoBackendServer:
        server = DemoBackendServer(name).start()
        servers.append(server)
        return server

    yield start

    for server in reversed(servers):
        server.stop()


@pytest.fixture
def gateway_factory():
    servers: list[GatewayServer] = []

    def start(
        backends: Iterable[DemoBackendServer],
        **overrides,
    ) -> GatewayServer:
        server = GatewayServer(make_config(backends, **overrides)).start_background()
        servers.append(server)
        return server

    yield start

    for server in reversed(servers):
        server.shutdown()


class HttpResponse:
    def __init__(
        self,
        status: int,
        headers: dict[str, str],
        body: bytes,
        duration: float,
    ) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        self.duration = duration

    @property
    def json(self):
        return json.loads(self.body.decode("utf-8"))

    @property
    def backend(self) -> str | None:
        return self.headers.get("x-backend")


def request(
    port: int,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 3.0,
) -> HttpResponse:
    started = time.perf_counter()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        response_headers = {
            key.lower(): value for key, value in response.getheaders()
        }
        return HttpResponse(
            status=int(response.status),
            headers=response_headers,
            body=data,
            duration=time.perf_counter() - started,
        )
    finally:
        connection.close()


def wait_until(
    predicate: Callable[[], bool], timeout: float = 3.0, interval: float = 0.01
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def wait_for_health(
    server: GatewayServer, backend_name: str, healthy: bool, timeout: float = 3.0
) -> bool:
    def check() -> bool:
        backend = server.app.by_name[backend_name]
        return backend.healthy is healthy

    return wait_until(check, timeout=timeout)


__all__ = [
    "HttpResponse",
    "backend_factory",
    "gateway_factory",
    "make_config",
    "request",
    "wait_for_health",
    "wait_until",
]
