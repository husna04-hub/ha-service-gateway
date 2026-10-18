from __future__ import annotations

from ha_gateway.metrics import Metrics, render_content_type


def test_metrics_render_request_and_histogram() -> None:
    metrics = Metrics()
    metrics.set_backend_up("api-1", True)
    metrics.record_request("GET", 200, "api-1", 0.032)
    metrics.record_request("POST", 503, "api-1", 1.5)
    text = metrics.render()
    assert "# TYPE ha_gateway_requests_total counter" in text
    assert 'ha_gateway_requests_total{method="GET",status="200",backend="api-1"} 1' in text
    assert 'ha_gateway_requests_total{method="POST",status="503",backend="api-1"} 1' in text
    assert 'ha_gateway_backend_up{backend="api-1"} 1' in text
    assert "ha_gateway_request_duration_seconds_bucket" in text
    assert 'le="+Inf"} 2' in text
    assert "ha_gateway_request_duration_seconds_count" in text


def test_metrics_failover_and_health() -> None:
    metrics = Metrics()
    metrics.record_failover("api-1", "api-2")
    metrics.record_transition("api-1", "unhealthy")
    metrics.record_health_check("api-1", False)
    metrics.set_ready(False)
    text = metrics.render()
    assert (
        'ha_gateway_failovers_total{from_backend="api-1",to_backend="api-2"} 1'
        in text
    )
    assert (
        'ha_gateway_backend_health_transitions_total{backend="api-1",state="unhealthy"} 1'
        in text
    )
    assert 'ha_gateway_health_checks_total{backend="api-1",result="fail"} 1' in text
    assert "ha_gateway_ready 0" in text


def test_metric_label_escaping() -> None:
    metrics = Metrics()
    metrics.record_request("GET", 200, 'api"one', 0.1)
    text = metrics.render()
    assert 'backend="api\\"one"' in text


def test_empty_metrics_still_has_content_type() -> None:
    assert "text/plain" in render_content_type()
    assert Metrics().render() == ""


def test_histogram_buckets_are_cumulative() -> None:
    metrics = Metrics()
    metrics.observe("ha_gateway_request_duration_seconds", ("api",), 0.001)
    metrics.observe("ha_gateway_request_duration_seconds", ("api",), 0.5)
    text = metrics.render()
    assert 'ha_gateway_request_duration_seconds_bucket{backend="api",le="0.005"} 1' in text
    assert 'ha_gateway_request_duration_seconds_bucket{backend="api",le="0.5"} 2' in text
    assert 'ha_gateway_request_duration_seconds_bucket{backend="api",le="+Inf"} 2' in text
