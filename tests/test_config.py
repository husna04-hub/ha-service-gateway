from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from ha_gateway.config import load_config, parse_config
from ha_gateway.errors import ConfigError


def base_config() -> dict:
    return {
        "version": 1,
        "listen": {"host": "127.0.0.1", "port": 8080},
        "request": {
            "timeout_seconds": 1.5,
            "max_attempts": 2,
            "retry_on_status": [502, 503, 503, 504],
        },
        "health": {
            "path": "/healthz",
            "interval_seconds": 0.5,
            "timeout_seconds": 0.3,
            "failure_threshold": 2,
            "success_threshold": 2,
            "initial_state": "healthy",
        },
        "backends": [
            {"name": "api-1", "url": "http://127.0.0.1:9001", "weight": 1},
            {"name": "api-2", "url": "https://api.example.test:9443/base/"},
        ],
        "metrics": {"enabled": True, "path": "/metrics"},
        "logging": {"level": "INFO", "format": "json"},
    }


def test_valid_config_normalizes_backends() -> None:
    config = parse_config(base_config())
    assert config.listen.port == 8080
    assert config.request.retry_on_status == (502, 503, 504)
    assert config.request.max_attempts == 2
    assert config.health.failure_threshold == 2
    assert config.backends[0].host == "127.0.0.1"
    assert config.backends[0].port == 9001
    assert config.backends[1].scheme == "https"
    assert config.backends[1].port == 9443
    assert config.backends[1].base_path == "/base"
    assert config.warnings == ()


def test_yaml_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "gateway.yaml"
    path.write_text(yaml.safe_dump(base_config()), encoding="utf-8")
    config = load_config(path)
    assert config.source_path == path.resolve()
    assert len(config.backends) == 2


def test_missing_config_has_hint(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "missing.yaml")
    assert "not found" in str(excinfo.value)
    assert excinfo.value.hint


def test_invalid_yaml_reports_location(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("backends:\n  - name: x\n   url: http://x\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(path)


def test_unknown_key_rejected() -> None:
    data = base_config()
    data["unexpected"] = True
    with pytest.raises(ConfigError, match="unknown key"):
        parse_config(data)


def test_duplicate_backend_names_rejected() -> None:
    data = base_config()
    data["backends"].append(copy.deepcopy(data["backends"][0]))
    with pytest.raises(ConfigError, match="duplicate backend"):
        parse_config(data)


def test_empty_backends_rejected() -> None:
    data = base_config()
    data["backends"] = []
    with pytest.raises(ConfigError, match="non-empty list"):
        parse_config(data)


def test_invalid_backend_scheme_rejected() -> None:
    data = base_config()
    data["backends"][0]["url"] = "ftp://127.0.0.1:9001"
    with pytest.raises(ConfigError, match="http or https"):
        parse_config(data)


def test_backend_credentials_rejected() -> None:
    data = base_config()
    data["backends"][0]["url"] = "http://user:pass@127.0.0.1:9001"
    with pytest.raises(ConfigError, match="credentials"):
        parse_config(data)


def test_invalid_weight_rejected() -> None:
    data = base_config()
    data["backends"][0]["weight"] = 0
    with pytest.raises(ConfigError, match="weight"):
        parse_config(data)


def test_retry_status_must_be_5xx() -> None:
    data = base_config()
    data["request"]["retry_on_status"] = [404]
    with pytest.raises(ConfigError, match="retry_on_status"):
        parse_config(data)


def test_invalid_initial_state_rejected() -> None:
    data = base_config()
    data["health"]["initial_state"] = "maybe"
    with pytest.raises(ConfigError, match="initial_state"):
        parse_config(data)


def test_metrics_path_conflict_rejected() -> None:
    data = base_config()
    data["metrics"]["path"] = "/healthz"
    with pytest.raises(ConfigError, match="conflicts"):
        parse_config(data)


def test_health_path_requires_leading_slash() -> None:
    data = base_config()
    data["health"]["path"] = "healthz"
    with pytest.raises(ConfigError, match="must start"):
        parse_config(data)


def test_warning_when_interval_shorter_than_timeout() -> None:
    data = base_config()
    data["health"]["interval_seconds"] = 0.1
    data["health"]["timeout_seconds"] = 0.5
    config = parse_config(data)
    assert any("interval_seconds" in warning for warning in config.warnings)


def test_summary_contains_backends() -> None:
    config = parse_config(base_config())
    summary = config.summary()
    assert summary["backends"][0]["name"] == "api-1"
    assert summary["health"]["path"] == "/healthz"
