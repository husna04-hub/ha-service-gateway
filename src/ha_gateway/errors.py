"""Typed, user-facing gateway errors."""

from __future__ import annotations


class GatewayError(Exception):
    exit_code = 1

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class ConfigError(GatewayError):
    exit_code = 2


class OperationalError(GatewayError):
    exit_code = 3
