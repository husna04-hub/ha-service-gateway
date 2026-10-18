"""Reverse-proxy gateway, weighted backend selection, and HTTP handlers."""

from __future__ import annotations

import http.client
import json
import logging
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable
from urllib.parse import urlsplit

from . import __version__
from .config import BackendConfig, GatewayConfig
from .health import HealthProber, HealthState
from .log import event, get_logger
from .metrics import Metrics, render_content_type

logger = get_logger("gateway")

IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"}
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
_FORWARD_REQUEST_SKIP = HOP_BY_HOP | {"host", "content-length", "x-request-id"}
_FORWARD_RESPONSE_SKIP = HOP_BY_HOP | {
    "content-length",
    "server",
    "date",
    "x-backend",
    "x-request-id",
    "x-gateway",
}
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


@dataclass
class BackendRuntime:
    config: BackendConfig
    health: HealthState
    current_weight: int = 0

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def healthy(self) -> bool:
        return self.health.healthy

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.config.name,
            "url": self.config.url,
            "weight": self.config.weight,
            "healthy": self.healthy,
            "state": self.health.state,
            "last_checked": self.health.last_checked,
            "last_error": self.health.last_error,
            "checks": self.health.checks,
            "failures": self.health.failures,
            "consecutive_failures": self.health.consecutive_failures,
            "consecutive_successes": self.health.consecutive_successes,
        }


class GatewayApp:
    """Owns backend health, weighted selection, metrics, and probe threads."""

    def __init__(self, config: GatewayConfig, metrics: Metrics | None = None) -> None:
        self.config = config
        self.metrics = metrics or Metrics()
        self.started_at = datetime.now(timezone.utc)
        self.backends: tuple[BackendRuntime, ...] = tuple(
            BackendRuntime(
                config=backend,
                health=HealthState(
                    failure_threshold=config.health.failure_threshold,
                    success_threshold=config.health.success_threshold,
                    state=config.health.initial_state,
                ),
            )
            for backend in config.backends
        )
        self.by_name = {backend.name: backend for backend in self.backends}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._probers: list[HealthProber] = []
        self._started = False
        self._inflight = 0
        for backend in self.backends:
            self.metrics.set_backend_up(backend.name, backend.healthy)
        self.metrics.set_ready(self._ready_locked())
        self.metrics.set_inflight(0)

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            for backend in self.backends:
                prober = HealthProber(
                    backend.config,
                    self.config.health,
                    callback=self.record_probe,
                    stop_event=self._stop_event,
                )
                self._probers.append(prober)
                prober.start()

    def stop(self) -> None:
        self._stop_event.set()
        for prober in self._probers:
            prober.join(timeout=3.0)
        self._probers.clear()

    def record_probe(self, name: str, ok: bool, detail: str) -> None:
        with self._lock:
            backend = self.by_name.get(name)
            if backend is None:
                return
            transition = backend.health.record(ok, detail)
            self.metrics.record_health_check(name, ok)
            self.metrics.set_backend_up(name, backend.healthy)
            self.metrics.set_ready(self._ready_locked())
            if transition:
                for item in self.backends:
                    item.current_weight = 0
                self.metrics.record_transition(name, transition)
                if transition == "healthy":
                    event(
                        logger,
                        logging.INFO,
                        "backend_recovered",
                        "backend recovered and is eligible for traffic",
                        backend=name,
                        detail=detail,
                    )
                else:
                    event(
                        logger,
                        logging.WARNING,
                        "backend_unhealthy",
                        "backend marked unhealthy and removed from rotation",
                        backend=name,
                        detail=detail,
                    )

    def select_backend(self, exclude: Iterable[str] = ()) -> BackendRuntime | None:
        excluded = set(exclude)
        with self._lock:
            candidates = [
                backend
                for backend in self.backends
                if backend.healthy and backend.name not in excluded
            ]
            if not candidates:
                return None
            total = sum(backend.config.weight for backend in candidates)
            for backend in candidates:
                backend.current_weight += backend.config.weight
            chosen = max(candidates, key=lambda item: item.current_weight)
            chosen.current_weight -= total
            return chosen

    def ready(self) -> bool:
        with self._lock:
            return self._ready_locked()

    def healthy_count(self) -> int:
        with self._lock:
            return sum(1 for backend in self.backends if backend.healthy)

    def begin_request(self) -> None:
        with self._lock:
            self._inflight += 1
            self.metrics.set_inflight(self._inflight)

    def end_request(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)
            self.metrics.set_inflight(self._inflight)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            ready = self._ready_locked()
            healthy = sum(1 for backend in self.backends if backend.healthy)
            return {
                "service": "ha-gateway",
                "version": __version__,
                "generated_at": _now(),
                "uptime_seconds": round(
                    (datetime.now(timezone.utc) - self.started_at).total_seconds(),
                    1,
                ),
                "ready": ready,
                "healthy_backends": healthy,
                "total_backends": len(self.backends),
                "backends": [backend.to_dict() for backend in self.backends],
            }

    def _ready_locked(self) -> bool:
        return any(backend.healthy for backend in self.backends)


class GatewayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], app: GatewayApp) -> None:
        self.app = app
        super().__init__(address, ProxyHandler)


class GatewayServer:
    """Convenience lifecycle wrapper used by the CLI, tests, and drill."""

    def __init__(self, config: GatewayConfig) -> None:
        self.app = GatewayApp(config)
        self.httpd = GatewayHTTPServer(
            (config.listen.host, config.listen.port), self.app
        )
        self.port = int(self.httpd.server_address[1])
        self._thread: threading.Thread | None = None

    def start_background(self) -> "GatewayServer":
        self.app.start()
        self._thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="gateway-http",
            daemon=True,
        )
        self._thread.start()
        return self

    def shutdown(self) -> None:
        if self._thread is not None:
            self.httpd.shutdown()
        self.httpd.server_close()
        self.app.stop()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def run(self, stop_event: threading.Event) -> None:
        self.app.start()
        self.httpd.timeout = 0.5
        try:
            while not stop_event.is_set():
                self.httpd.handle_request()
        finally:
            self.httpd.server_close()
            self.app.stop()

    def wait_ready(self, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.app.ready():
                return True
            time.sleep(0.01)
        return self.app.ready()


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    reason: str
    headers: list[tuple[str, str]]
    body: bytes


class UpstreamFailure(Exception):
    pass


class UpstreamTimeout(UpstreamFailure):
    pass


class UpstreamError(UpstreamFailure):
    pass


class RequestError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"ha-gateway/{__version__}"
    sys_version = ""

    @property
    def app(self) -> GatewayApp:
        return self.server.app  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch()

    def log_message(self, *args: object) -> None:
        return

    def _dispatch(self) -> None:
        self._request_id = _request_id(
            self.headers.get("X-Request-Id")
        )
        self._response_started = False
        self._status_sent = 0
        self._backend_name = "none"
        self._attempts = 0
        self._request_path = "/"
        method = self.command
        started = time.perf_counter()
        self.app.begin_request()
        try:
            self._handle_request()
        except Exception:
            logger.exception(
                "unhandled gateway error",
                extra={
                    "event": "request_exception",
                    "fields": {
                        "request_id": self._request_id,
                        "method": method,
                        "path": self._request_path,
                    },
                },
            )
            if not self._response_started:
                self._send_error_json(
                    500,
                    "internal_error",
                    "The gateway encountered an unexpected error.",
                )
        finally:
            self.app.end_request()
            status = self._status_sent or 500
            duration = time.perf_counter() - started
            self.app.metrics.record_request(
                method, status, self._backend_name, duration
            )
            event(
                logger,
                logging.INFO if status < 500 else logging.WARNING,
                "request_completed",
                "request completed",
                request_id=self._request_id,
                method=method,
                path=self._request_path,
                status=status,
                backend=self._backend_name,
                attempts=self._attempts,
                duration_ms=round(duration * 1000, 2),
                client=self.client_address[0],
            )

    def _handle_request(self) -> None:
        try:
            parts = urlsplit(self.path)
        except ValueError:
            self._send_error_json(400, "bad_request", "Malformed request URL.")
            return
        path = parts.path or "/"
        query = parts.query
        self._request_path = path

        if path == "/healthz":
            if not self._require_get():
                return
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "ha-gateway",
                    "version": __version__,
                },
            )
            return

        if path == "/readyz":
            if not self._require_get():
                return
            ready = self.app.ready()
            self._send_json(
                200 if ready else 503,
                {
                    "ready": ready,
                    "healthy_backends": self.app.healthy_count(),
                    "total_backends": len(self.app.backends),
                },
            )
            return

        if path == self.app.config.metrics.path:
            if not self.app.config.metrics.enabled:
                self._send_error_json(404, "not_found", "Metrics are disabled.")
                return
            if not self._require_get():
                return
            self._send_bytes(
                200,
                self.app.metrics.render().encode("utf-8"),
                headers=[("Content-Type", render_content_type())],
            )
            return

        if path == "/admin/backends":
            if not self._require_get():
                return
            self._send_json(200, self.app.snapshot())
            return

        self._proxy(path, query)

    def _proxy(self, path: str, query: str) -> None:
        try:
            body = self._read_body()
        except RequestError as exc:
            self._send_error_json(exc.status, exc.code, exc.message)
            return

        target = path
        if query:
            target = f"{target}?{query}"
        headers = self._forward_headers()
        max_attempts = self.app.config.request.max_attempts
        excluded: set[str] = set()
        previous: str | None = None
        last_failure: tuple[int, str, str] | None = None
        attempts = 0

        while attempts < max_attempts:
            backend = self.app.select_backend(excluded)
            if backend is None:
                break
            attempts += 1
            if previous is not None:
                self.app.metrics.record_failover(previous, backend.name)
            self._attempts = attempts
            self._backend_name = backend.name
            try:
                upstream = self._forward(backend, target, body, headers)
            except UpstreamTimeout as exc:
                last_failure = (
                    504,
                    "upstream_timeout",
                    f"Backend {backend.name} timed out: {exc}",
                )
                self.app.metrics.record_upstream_failure(backend.name, "timeout")
                self._log_failure(backend.name, "timeout", str(exc), attempts)
                excluded.add(backend.name)
                previous = backend.name
                continue
            except UpstreamError as exc:
                last_failure = (
                    502,
                    "upstream_error",
                    f"Backend {backend.name} failed: {exc}",
                )
                self.app.metrics.record_upstream_failure(backend.name, "connect")
                self._log_failure(backend.name, "connect", str(exc), attempts)
                excluded.add(backend.name)
                previous = backend.name
                continue

            if (
                upstream.status in self.app.config.request.retry_on_status
                and self.command in IDEMPOTENT_METHODS
                and attempts < max_attempts
            ):
                last_failure = (
                    upstream.status,
                    "upstream_status",
                    f"Backend {backend.name} returned HTTP {upstream.status}.",
                )
                self.app.metrics.record_upstream_failure(
                    backend.name, f"status_{upstream.status}"
                )
                self._log_failure(
                    backend.name,
                    f"status_{upstream.status}",
                    "retryable upstream response",
                    attempts,
                )
                excluded.add(backend.name)
                previous = backend.name
                continue

            self._backend_name = backend.name
            self._send_upstream(upstream, head=self.command == "HEAD")
            return

        if attempts == 0:
            self._attempts = 0
            self._backend_name = "none"
            self._send_error_json(
                503,
                "no_healthy_backends",
                "No healthy backends are currently available.",
                extra_headers=[("Retry-After", "1")],
            )
            return

        status, code, message = last_failure or (
            502,
            "upstream_error",
            "All backend attempts failed.",
        )
        self._send_error_json(status, code, message)

    def _read_body(self) -> bytes:
        transfer_encoding = self.headers.get("Transfer-Encoding")
        if transfer_encoding:
            raise RequestError(
                501,
                "unsupported_transfer_encoding",
                "Chunked request bodies are not supported by this prototype.",
            )
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b""
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise RequestError(
                400, "invalid_content_length", "Invalid Content-Length header."
            ) from exc
        if length < 0:
            raise RequestError(
                400, "invalid_content_length", "Content-Length must be non-negative."
            )
        if length > self.app.config.request.max_body_bytes:
            raise RequestError(
                413,
                "request_too_large",
                "Request body exceeds the configured max_body_bytes.",
            )
        data = self.rfile.read(length)
        if len(data) != length:
            raise RequestError(
                400, "incomplete_body", "Request body ended before Content-Length."
            )
        return data

    def _forward_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in self.headers.items():
            if key.lower() in _FORWARD_REQUEST_SKIP:
                continue
            headers[key] = value
        client_ip = self.client_address[0]
        forwarded_for = self.headers.get("X-Forwarded-For")
        headers["X-Forwarded-For"] = (
            f"{forwarded_for}, {client_ip}" if forwarded_for else client_ip
        )
        headers["X-Forwarded-Proto"] = "http"
        headers["X-Forwarded-Host"] = self.headers.get("Host", "")
        headers["X-Request-Id"] = self._request_id
        headers["Connection"] = "close"
        return headers

    def _forward(
        self,
        backend: BackendRuntime,
        target: str,
        body: bytes,
        headers: dict[str, str],
    ) -> UpstreamResponse:
        config = self.app.config.request
        connection_class = (
            http.client.HTTPSConnection
            if backend.config.scheme == "https"
            else http.client.HTTPConnection
        )
        connection: http.client.HTTPConnection | None = None
        try:
            connection = connection_class(
                backend.config.host,
                backend.config.port,
                timeout=config.timeout_seconds,
            )
            upstream_path = f"{backend.config.base_path}{target}"
            if not upstream_path.startswith("/"):
                upstream_path = "/" + upstream_path
            connection.request(
                self.command,
                upstream_path,
                body=body if body else None,
                headers=headers,
            )
            response = connection.getresponse()
            data = response.read(config.max_response_bytes + 1)
            if len(data) > config.max_response_bytes:
                raise UpstreamError(
                    "response exceeds max_response_bytes"
                )
            return UpstreamResponse(
                status=int(response.status),
                reason=response.reason or "",
                headers=list(response.getheaders()),
                body=data,
            )
        except (TimeoutError, socket.timeout) as exc:
            raise UpstreamTimeout(
                f"timed out after {config.timeout_seconds:g}s"
            ) from exc
        except (http.client.HTTPException, OSError) as exc:
            raise UpstreamError(str(exc)) from exc
        finally:
            if connection is not None:
                connection.close()

    def _send_upstream(self, upstream: UpstreamResponse, *, head: bool) -> None:
        headers = [
            (key, value)
            for key, value in upstream.headers
            if key.lower() not in _FORWARD_RESPONSE_SKIP
        ]
        self._send_bytes(
            upstream.status,
            upstream.body,
            headers=headers,
            head=head,
            reason=upstream.reason or None,
            extra_headers=[("X-Backend", self._backend_name)],
        )

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send_bytes(
            status,
            body,
            headers=[("Content-Type", "application/json; charset=utf-8")],
        )

    def _send_error_json(
        self,
        status: int,
        code: str,
        message: str,
        *,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        payload = {
            "error": code,
            "message": message,
            "request_id": self._request_id,
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = [("Content-Type", "application/json; charset=utf-8")]
        headers.extend(extra_headers or [])
        self._send_bytes(status, body, headers=headers)

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        *,
        headers: list[tuple[str, str]] | None = None,
        head: bool | None = None,
        reason: str | None = None,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        if self._response_started:
            return
        if head is None:
            head = self.command == "HEAD"
        self._response_started = True
        self._status_sent = status
        self.send_response(status, reason)
        for key, value in headers or []:
            self.send_header(key, value)
        for key, value in extra_headers or []:
            self.send_header(key, value)
        self.send_header("X-Request-Id", self._request_id)
        self.send_header("X-Gateway", f"ha-gateway/{__version__}")
        self.send_header("X-Gateway-Attempts", str(self._attempts))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if not head and body:
            self.wfile.write(body)

    def _require_get(self) -> bool:
        if self.command in {"GET", "HEAD"}:
            return True
        self._send_error_json(
            405,
            "method_not_allowed",
            "This endpoint only supports GET and HEAD.",
            extra_headers=[("Allow", "GET, HEAD")],
        )
        return False

    def _log_failure(
        self, backend: str, reason: str, detail: str, attempt: int
    ) -> None:
        event(
            logger,
            logging.WARNING,
            "upstream_failure",
            "upstream attempt failed",
            request_id=self._request_id,
            backend=backend,
            reason=reason,
            detail=detail,
            attempt=attempt,
        )


def _request_id(candidate: str | None) -> str:
    if candidate and _REQUEST_ID_RE.match(candidate):
        return candidate
    return uuid.uuid4().hex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
