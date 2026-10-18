"""Thread-safe Prometheus metrics with a small, dependency-free exposition."""

from __future__ import annotations

import math
import threading
from typing import Iterable

BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

_METADATA: dict[str, tuple[str, str]] = {
    "ha_gateway_requests_total": (
        "HTTP requests handled by the gateway, by final status and backend.",
        "counter",
    ),
    "ha_gateway_request_duration_seconds": (
        "End-to-end gateway request latency in seconds.",
        "histogram",
    ),
    "ha_gateway_upstream_failures_total": (
        "Upstream request failures by backend and reason.",
        "counter",
    ),
    "ha_gateway_health_checks_total": (
        "Active health probes by backend and result.",
        "counter",
    ),
    "ha_gateway_backend_health_transitions_total": (
        "Backend health state transitions.",
        "counter",
    ),
    "ha_gateway_failovers_total": (
        "Requests retried against a different backend.",
        "counter",
    ),
    "ha_gateway_backend_up": (
        "Whether a backend is currently healthy (1) or unhealthy (0).",
        "gauge",
    ),
    "ha_gateway_inflight_requests": (
        "Requests currently being handled by the gateway.",
        "gauge",
    ),
    "ha_gateway_ready": (
        "Whether at least one healthy backend is available (1) or not (0).",
        "gauge",
    ),
}

_LABELS: dict[str, tuple[str, ...]] = {
    "ha_gateway_requests_total": ("method", "status", "backend"),
    "ha_gateway_request_duration_seconds": ("backend",),
    "ha_gateway_upstream_failures_total": ("backend", "reason"),
    "ha_gateway_health_checks_total": ("backend", "result"),
    "ha_gateway_backend_health_transitions_total": ("backend", "state"),
    "ha_gateway_failovers_total": ("from_backend", "to_backend"),
    "ha_gateway_backend_up": ("backend",),
    "ha_gateway_inflight_requests": (),
    "ha_gateway_ready": (),
}


class _Histogram:
    def __init__(self) -> None:
        self.counts = [0 for _ in BUCKETS]
        self.total = 0.0
        self.count = 0

    def observe(self, value: float) -> None:
        if math.isnan(value) or value < 0:
            return
        self.total += value
        self.count += 1
        for index, boundary in enumerate(BUCKETS):
            if value <= boundary:
                self.counts[index] += 1
                break

    def render(self, name: str, labels: str) -> list[str]:
        lines: list[str] = []
        cumulative = 0
        for boundary, count in zip(BUCKETS, self.counts):
            cumulative += count
            lines.append(
                f'{name}_bucket{_with_label(labels, "le", _fmt(boundary))} '
                f"{cumulative}"
            )
        lines.append(
            f'{name}_bucket{_with_label(labels, "le", "+Inf")} {self.count}'
        )
        lines.append(f"{name}_sum{labels} {_fmt(self.total)}")
        lines.append(f"{name}_count{labels} {self.count}")
        return lines


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counters: dict[str, dict[tuple[str, ...], float]] = {}
        self._gauges: dict[str, dict[tuple[str, ...], float]] = {}
        self._histograms: dict[str, dict[tuple[str, ...], _Histogram]] = {}

    def inc_counter(
        self, name: str, labels: tuple[str, ...], amount: float = 1.0
    ) -> None:
        with self._lock:
            family = self._counters.setdefault(name, {})
            family[labels] = family.get(labels, 0.0) + amount

    def set_gauge(
        self, name: str, labels: tuple[str, ...], value: float
    ) -> None:
        with self._lock:
            self._gauges.setdefault(name, {})[labels] = value

    def observe(
        self, name: str, labels: tuple[str, ...], value: float
    ) -> None:
        with self._lock:
            family = self._histograms.setdefault(name, {})
            histogram = family.get(labels)
            if histogram is None:
                histogram = _Histogram()
                family[labels] = histogram
            histogram.observe(value)

    # Typed helpers keep call sites readable and label order correct.

    def record_request(
        self,
        method: str,
        status: int,
        backend: str,
        duration_seconds: float,
    ) -> None:
        self.inc_counter(
            "ha_gateway_requests_total",
            (method, str(status), backend),
        )
        self.observe(
            "ha_gateway_request_duration_seconds",
            (backend,),
            duration_seconds,
        )

    def record_upstream_failure(self, backend: str, reason: str) -> None:
        self.inc_counter(
            "ha_gateway_upstream_failures_total", (backend, reason)
        )

    def record_health_check(self, backend: str, ok: bool) -> None:
        self.inc_counter(
            "ha_gateway_health_checks_total",
            (backend, "ok" if ok else "fail"),
        )

    def record_transition(self, backend: str, state: str) -> None:
        self.inc_counter(
            "ha_gateway_backend_health_transitions_total",
            (backend, state),
        )

    def record_failover(self, from_backend: str, to_backend: str) -> None:
        self.inc_counter(
            "ha_gateway_failovers_total", (from_backend, to_backend)
        )

    def set_backend_up(self, backend: str, up: bool) -> None:
        self.set_gauge("ha_gateway_backend_up", (backend,), 1.0 if up else 0.0)

    def set_ready(self, ready: bool) -> None:
        self.set_gauge("ha_gateway_ready", (), 1.0 if ready else 0.0)

    def set_inflight(self, value: int) -> None:
        self.set_gauge("ha_gateway_inflight_requests", (), float(value))

    def render(self) -> str:
        with self._lock:
            lines: list[str] = []
            for name in _METADATA:
                help_text, metric_type = _METADATA[name]
                family = (
                    self._counters.get(name)
                    if metric_type == "counter"
                    else self._histograms.get(name)
                    if metric_type == "histogram"
                    else self._gauges.get(name)
                )
                if not family:
                    continue
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} {metric_type}")
                for labels, value in sorted(family.items()):
                    label_text = _label_text(name, labels)
                    if metric_type == "histogram":
                        lines.extend(value.render(name, label_text))
                    else:
                        lines.append(f"{name}{label_text} {_fmt(value)}")
            return "\n".join(lines) + "\n" if lines else ""


def _label_text(name: str, values: Iterable[str]) -> str:
    names = _LABELS[name]
    pairs = list(zip(names, values))
    if not pairs:
        return ""
    rendered = ",".join(
        f'{key}="{_escape(str(value))}"' for key, value in pairs
    )
    return "{" + rendered + "}"


def _with_label(label_text: str, key: str, value: str) -> str:
    if not label_text:
        return f'{{{key}="{_escape(value)}"}}'
    return f'{label_text[:-1]},{key}="{_escape(value)}"}}'


def _escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _fmt(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.12g}"


def render_content_type() -> str:
    return "text/plain; version=0.0.4; charset=utf-8"
