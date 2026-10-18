from __future__ import annotations

import json
from pathlib import Path

import yaml

from ha_gateway.cli import main


def write_config(path: Path, **overrides) -> Path:
    data = {
        "version": 1,
        "listen": {"host": "127.0.0.1", "port": 0},
        "request": {"timeout_seconds": 0.5, "max_attempts": 2},
        "health": {
            "path": "/healthz",
            "interval_seconds": 0.1,
            "timeout_seconds": 0.2,
            "failure_threshold": 2,
            "success_threshold": 2,
        },
        "backends": [{"name": "api", "url": "http://127.0.0.1:9001"}],
        "metrics": {"enabled": True, "path": "/metrics"},
        "logging": {"level": "ERROR", "format": "json"},
    }
    data.update(overrides)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_check_config_json(capsys, tmp_path: Path) -> None:
    config = write_config(tmp_path / "gateway.yaml")
    code = main(["--config", str(config), "--check-config", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["listen"]["port"] == 0
    assert payload["backends"][0]["name"] == "api"


def test_check_config_human_readable(capsys, tmp_path: Path) -> None:
    config = write_config(tmp_path / "gateway.yaml")
    code = main(["--config", str(config), "--check-config"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Configuration valid" in out
    assert "Backends: 1" in out


def test_invalid_config_returns_2(capsys, tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("version: 1\nbackends: []\n", encoding="utf-8")
    code = main(["--config", str(path), "--check-config"])
    captured = capsys.readouterr()
    assert code == 2
    assert "error:" in captured.err
    assert "hint:" in captured.err


def test_host_and_port_override(capsys, tmp_path: Path) -> None:
    config = write_config(tmp_path / "gateway.yaml")
    code = main(
        [
            "--config",
            str(config),
            "--check-config",
            "--json",
            "--host",
            "127.0.0.1",
            "--port",
            "12345",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["listen"] == {"host": "127.0.0.1", "port": 12345}
