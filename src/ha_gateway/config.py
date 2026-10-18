"""Strict gateway configuration schema and YAML loader."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml
from yaml import YAMLError

from .errors import ConfigError

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

ROOT_KEYS = {"version", "listen", "request", "health", "backends", "metrics", "logging"}
RESERVED_PATHS = {"/healthz", "/readyz", "/admin/backends"}


@dataclass(frozen=True)
class ListenConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass(frozen=True)
class RequestConfig:
    timeout_seconds: float = 2.0
    max_attempts: int = 3
    retry_on_status: tuple[int, ...] = (502, 503, 504)
    max_body_bytes: int = 10 * 1024 * 1024
    max_response_bytes: int = 10 * 1024 * 1024


@dataclass(frozen=True)
class HealthConfig:
    path: str = "/healthz"
    interval_seconds: float = 2.0
    timeout_seconds: float = 1.0
    failure_threshold: int = 3
    success_threshold: int = 2
    initial_state: str = "healthy"


@dataclass(frozen=True)
class MetricsConfig:
    enabled: bool = True
    path: str = "/metrics"


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    format: str = "json"


@dataclass(frozen=True)
class BackendConfig:
    name: str
    url: str
    weight: int = 1
    scheme: str = field(init=False, compare=False)
    host: str = field(init=False, compare=False)
    port: int = field(init=False, compare=False)
    base_path: str = field(init=False, compare=False)

    def __post_init__(self) -> None:
        parts = urlsplit(self.url)
        scheme = parts.scheme.lower()
        port = parts.port or (443 if scheme == "https" else 80)
        path = parts.path.rstrip("/")
        object.__setattr__(self, "scheme", scheme)
        object.__setattr__(self, "host", parts.hostname or "")
        object.__setattr__(self, "port", int(port))
        object.__setattr__(self, "base_path", "" if path == "/" else path)


@dataclass(frozen=True)
class GatewayConfig:
    backends: tuple[BackendConfig, ...]
    listen: ListenConfig = field(default_factory=ListenConfig)
    request: RequestConfig = field(default_factory=RequestConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    warnings: tuple[str, ...] = ()
    source_path: Path | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "version": 1,
            "source": str(self.source_path) if self.source_path else None,
            "listen": {"host": self.listen.host, "port": self.listen.port},
            "backends": [
                {"name": backend.name, "url": backend.url, "weight": backend.weight}
                for backend in self.backends
            ],
            "health": {
                "path": self.health.path,
                "interval_seconds": self.health.interval_seconds,
                "timeout_seconds": self.health.timeout_seconds,
                "failure_threshold": self.health.failure_threshold,
                "success_threshold": self.health.success_threshold,
                "initial_state": self.health.initial_state,
            },
            "request": {
                "timeout_seconds": self.request.timeout_seconds,
                "max_attempts": self.request.max_attempts,
                "retry_on_status": list(self.request.retry_on_status),
            },
            "metrics": {
                "enabled": self.metrics.enabled,
                "path": self.metrics.path,
            },
            "warnings": list(self.warnings),
        }


def load_config(path: str | Path) -> GatewayConfig:
    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise ConfigError(
            f"configuration file not found: {config_path}",
            hint="Pass --config with a path to an existing YAML file.",
        )
    if not config_path.is_file():
        raise ConfigError(
            f"configuration path is not a file: {config_path}",
            hint="Point --config at a YAML file, not a directory.",
        )
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"could not read configuration {config_path}: {exc}",
            hint="Check file permissions and that the file is valid UTF-8.",
        ) from exc
    try:
        data = yaml.safe_load(text)
    except YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = (
            f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        )
        problem = getattr(exc, "problem", None) or str(exc)
        raise ConfigError(
            f"invalid YAML{location}: {problem}",
            hint="Fix the YAML syntax (indentation and quoting are common causes).",
        ) from exc
    return parse_config(data, config_path.resolve())


def parse_config(data: Any, source_path: Path | None = None) -> GatewayConfig:
    root = _mapping(data, "configuration")
    _check_keys(root, ROOT_KEYS, "configuration")
    _integer(root.get("version"), "version", minimum=1, maximum=1)

    listen_data = _mapping(root.get("listen", {}), "listen")
    _check_keys(listen_data, {"host", "port"}, "listen")
    listen = ListenConfig(
        host=_string(
            listen_data.get("host", "0.0.0.0"),
            "listen.host",
            max_length=253,
        ),
        port=_integer(
            listen_data.get("port", 8080),
            "listen.port",
            minimum=0,
            maximum=65535,
        ),
    )

    request_data = _mapping(root.get("request", {}), "request")
    _check_keys(
        request_data,
        {
            "timeout_seconds",
            "max_attempts",
            "retry_on_status",
            "max_body_bytes",
            "max_response_bytes",
        },
        "request",
    )
    request = RequestConfig(
        timeout_seconds=_number(
            request_data.get("timeout_seconds", 2.0),
            "request.timeout_seconds",
            minimum=0.05,
            maximum=300.0,
        ),
        max_attempts=_integer(
            request_data.get("max_attempts", 3),
            "request.max_attempts",
            minimum=1,
            maximum=10,
        ),
        retry_on_status=_status_codes(
            request_data.get("retry_on_status", [502, 503, 504]),
            "request.retry_on_status",
        ),
        max_body_bytes=_integer(
            request_data.get("max_body_bytes", 10 * 1024 * 1024),
            "request.max_body_bytes",
            minimum=1024,
            maximum=100 * 1024 * 1024,
        ),
        max_response_bytes=_integer(
            request_data.get("max_response_bytes", 10 * 1024 * 1024),
            "request.max_response_bytes",
            minimum=1024,
            maximum=100 * 1024 * 1024,
        ),
    )

    health_data = _mapping(root.get("health", {}), "health")
    _check_keys(
        health_data,
        {
            "path",
            "interval_seconds",
            "timeout_seconds",
            "failure_threshold",
            "success_threshold",
            "initial_state",
        },
        "health",
    )
    initial_state = _string(
        health_data.get("initial_state", "healthy"),
        "health.initial_state",
    ).lower()
    if initial_state not in {"healthy", "unhealthy"}:
        raise ConfigError(
            "health.initial_state must be 'healthy' or 'unhealthy'",
            hint="Use unhealthy to require a successful probe before serving traffic.",
        )
    health = HealthConfig(
        path=_path(health_data.get("path", "/healthz"), "health.path"),
        interval_seconds=_number(
            health_data.get("interval_seconds", 2.0),
            "health.interval_seconds",
            minimum=0.05,
            maximum=300.0,
        ),
        timeout_seconds=_number(
            health_data.get("timeout_seconds", 1.0),
            "health.timeout_seconds",
            minimum=0.05,
            maximum=300.0,
        ),
        failure_threshold=_integer(
            health_data.get("failure_threshold", 3),
            "health.failure_threshold",
            minimum=1,
            maximum=20,
        ),
        success_threshold=_integer(
            health_data.get("success_threshold", 2),
            "health.success_threshold",
            minimum=1,
            maximum=20,
        ),
        initial_state=initial_state,
    )

    metrics_data = _mapping(root.get("metrics", {}), "metrics")
    _check_keys(metrics_data, {"enabled", "path"}, "metrics")
    metrics = MetricsConfig(
        enabled=_boolean(metrics_data.get("enabled", True), "metrics.enabled"),
        path=_path(metrics_data.get("path", "/metrics"), "metrics.path"),
    )
    if metrics.path in RESERVED_PATHS:
        raise ConfigError(
            f"metrics.path conflicts with a built-in endpoint: {metrics.path}",
            hint="Choose a different path such as /metrics or /_metrics.",
        )
    if metrics.path == health.path:
        raise ConfigError(
            "metrics.path must differ from health.path",
            hint="Use a dedicated metrics path such as /metrics.",
        )

    logging_data = _mapping(root.get("logging", {}), "logging")
    _check_keys(logging_data, {"level", "format"}, "logging")
    log_level = _string(
        logging_data.get("level", "INFO"), "logging.level"
    ).upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError(
            f"logging.level is invalid: {log_level}",
            hint="Use DEBUG, INFO, WARNING, ERROR, or CRITICAL.",
        )
    log_format = _string(
        logging_data.get("format", "json"), "logging.format"
    ).lower()
    if log_format not in {"json", "text"}:
        raise ConfigError(
            f"logging.format is invalid: {log_format}",
            hint="Use json or text.",
        )
    logging_config = LoggingConfig(level=log_level, format=log_format)

    raw_backends = root.get("backends")
    if not isinstance(raw_backends, list) or not raw_backends:
        raise ConfigError(
            "backends must be a non-empty list",
            hint="Add at least one backend URL such as http://127.0.0.1:9001.",
        )
    backends: list[BackendConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_backends):
        backend = _parse_backend(item, index)
        if backend.name in seen:
            raise ConfigError(
                f"duplicate backend name: {backend.name}",
                hint="Backend names must be unique; rename one of the entries.",
            )
        seen.add(backend.name)
        backends.append(backend)

    warnings: list[str] = []
    if health.interval_seconds < health.timeout_seconds:
        warnings.append(
            "health.interval_seconds is smaller than health.timeout_seconds; "
            "probes may run back-to-back under slow responses"
        )
    if request.max_attempts > len(backends):
        warnings.append(
            "request.max_attempts exceeds the number of backends; "
            "extra attempts cannot select a new backend"
        )

    return GatewayConfig(
        backends=tuple(backends),
        listen=listen,
        request=request,
        health=health,
        metrics=metrics,
        logging=logging_config,
        warnings=tuple(warnings),
        source_path=source_path,
    )


def _parse_backend(item: Any, index: int) -> BackendConfig:
    where = f"backends[{index}]"
    data = _mapping(item, where)
    _check_keys(data, {"name", "url", "weight"}, where)
    name = _string(
        data.get("name"),
        f"{where}.name",
        pattern=_NAME_RE,
        pattern_hint="letters, digits, dot, dash, underscore",
    )
    url = _string(data.get("url"), f"{where}.url", max_length=2048)
    weight = _integer(
        data.get("weight", 1), f"{where}.weight", minimum=1, maximum=100
    )
    _validate_backend_url(url, f"{where}.url")
    return BackendConfig(name=name, url=url, weight=weight)


def _validate_backend_url(url: str, where: str) -> None:
    if any(char in url for char in "\r\n"):
        raise ConfigError(
            f"{where} contains a newline", hint="URLs must be a single line."
        )
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ConfigError(
            f"{where} is not a valid URL: {exc}",
            hint="Use a URL such as http://127.0.0.1:9001.",
        ) from exc
    if parts.scheme.lower() not in {"http", "https"}:
        raise ConfigError(
            f"{where} must use http or https",
            hint="Backend URLs must include a supported scheme.",
        )
    if not parts.hostname:
        raise ConfigError(
            f"{where} must include a hostname",
            hint="Use a URL such as http://backend-a:9001.",
        )
    if parts.username or parts.password:
        raise ConfigError(
            f"{where} must not embed credentials",
            hint="Remove user:password from the URL; use a trusted network boundary.",
        )
    if parts.query:
        raise ConfigError(
            f"{where} must not contain a query string",
            hint="Move query parameters to the client request.",
        )
    if parts.fragment:
        raise ConfigError(
            f"{where} must not contain a fragment",
            hint="Remove the #... suffix.",
        )
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError(
            f"{where} has an invalid port",
            hint="Use a port between 1 and 65535.",
        )
    if any(char.isspace() for char in url):
        raise ConfigError(
            f"{where} contains whitespace",
            hint="URLs must not contain spaces.",
        )


def _status_codes(value: Any, where: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(
            f"{where} must be a non-empty list of HTTP status codes",
            hint="For example: [502, 503, 504].",
        )
    codes = tuple(
        _integer(item, f"{where}[{index}]", minimum=500, maximum=599)
        for index, item in enumerate(value)
    )
    return tuple(sorted(set(codes)))


def _path(value: Any, where: str) -> str:
    path = _string(value, where, max_length=1024)
    if not path.startswith("/"):
        raise ConfigError(
            f"{where} must start with '/'",
            hint="Example: /healthz.",
        )
    if "?" in path or "#" in path or " " in path:
        raise ConfigError(
            f"{where} must be a plain URL path",
            hint="Do not include a query string, fragment, or spaces.",
        )
    return path


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(
            f"{where} must be a mapping",
            hint=f"Use YAML key/value pairs for {where}.",
        )
    return value


def _check_keys(data: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(
            f"unknown key(s) at {where}: " + ", ".join(str(key) for key in unknown),
            hint="Remove the key(s) or check the configuration reference in README.md.",
        )


def _string(
    value: Any,
    where: str,
    *,
    max_length: int = 500,
    pattern: re.Pattern[str] | None = None,
    pattern_hint: str = "",
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"{where} must be a non-empty string",
            hint="Quote values that contain YAML-special characters.",
        )
    if len(value) > max_length:
        raise ConfigError(
            f"{where} is too long ({len(value)} > {max_length} characters)",
            hint="Shorten the value.",
        )
    if pattern is not None and not pattern.match(value):
        raise ConfigError(
            f"{where} has invalid characters: {value!r}",
            hint=f"Use only {pattern_hint}.",
        )
    return value


def _integer(value: Any, where: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"{where} must be an integer",
            hint=f"Use a whole number between {minimum} and {maximum}.",
        )
    if not minimum <= value <= maximum:
        raise ConfigError(
            f"{where} must be between {minimum} and {maximum}",
            hint=f"Got {value}.",
        )
    return value


def _number(value: Any, where: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f"{where} must be a number",
            hint=f"Use a value between {minimum} and {maximum}.",
        )
    number = float(value)
    if not math.isfinite(number):
        raise ConfigError(f"{where} must be finite", hint="Use a decimal value.")
    if not minimum <= number <= maximum:
        raise ConfigError(
            f"{where} must be between {minimum} and {maximum}",
            hint=f"Got {number}.",
        )
    return number


def _boolean(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(
            f"{where} must be true or false",
            hint="YAML booleans are unquoted true/false.",
        )
    return value
