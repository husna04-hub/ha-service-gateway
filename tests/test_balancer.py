from __future__ import annotations

from collections import Counter

from ha_gateway.config import (
    BackendConfig,
    GatewayConfig,
    HealthConfig,
    ListenConfig,
    LoggingConfig,
    MetricsConfig,
    RequestConfig,
)
from ha_gateway.gateway import GatewayApp


def make_app(
    weights: list[tuple[str, int]] | None = None, **health_kwargs
) -> GatewayApp:
    weights = weights or [("a", 1), ("b", 1), ("c", 1)]
    config = GatewayConfig(
        backends=tuple(
            BackendConfig(
                name=name,
                url=f"http://127.0.0.1:{9100 + index}",
                weight=weight,
            )
            for index, (name, weight) in enumerate(weights)
        ),
        listen=ListenConfig("127.0.0.1", 0),
        request=RequestConfig(),
        health=HealthConfig(
            failure_threshold=health_kwargs.get("failure_threshold", 2),
            success_threshold=2,
        ),
        metrics=MetricsConfig(),
        logging=LoggingConfig(level="ERROR"),
    )
    return GatewayApp(config)


def test_round_robin_distribution() -> None:
    app = make_app()
    counts = Counter(
        app.select_backend().name for _ in range(30)  # type: ignore[union-attr]
    )
    assert counts == {"a": 10, "b": 10, "c": 10}


def test_weighted_distribution() -> None:
    app = make_app([("a", 1), ("b", 3)])
    counts = Counter(
        app.select_backend().name for _ in range(40)  # type: ignore[union-attr]
    )
    assert counts["b"] == 30
    assert counts["a"] == 10


def test_exclusion_is_respected() -> None:
    app = make_app()
    for _ in range(10):
        backend = app.select_backend(exclude={"a"})
        assert backend is not None
        assert backend.name != "a"
    assert app.select_backend(exclude={"a", "b", "c"}) is None


def test_unhealthy_backend_is_skipped() -> None:
    app = make_app()
    app.record_probe("a", False, "down")
    app.record_probe("a", False, "down again")
    assert app.by_name["a"].healthy is False
    assert all(app.select_backend().name != "a" for _ in range(10))  # type: ignore[union-attr]


def test_readiness_tracks_health() -> None:
    app = make_app(failure_threshold=1)
    assert app.ready() is True
    for name in ("a", "b", "c"):
        app.record_probe(name, False, "down")
    assert app.ready() is False
    assert app.snapshot()["ready"] is False
    assert app.snapshot()["healthy_backends"] == 0


def test_snapshot_contains_backend_state() -> None:
    app = make_app()
    snapshot = app.snapshot()
    assert snapshot["total_backends"] == 3
    assert snapshot["healthy_backends"] == 3
    assert snapshot["backends"][0]["name"] == "a"
    assert "last_checked" in snapshot["backends"][0]
