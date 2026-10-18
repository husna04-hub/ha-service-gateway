"""ha-gateway command-line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import GatewayConfig, load_config
from .errors import GatewayError, OperationalError
from .gateway import GatewayServer
from .log import configure_logging, event, get_logger

logger = get_logger("cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ha-gateway",
        description=(
            "High-availability HTTP gateway with active health checks, "
            "weighted backend selection, retries, and failover."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("config/gateway.yaml"),
        help="Path to the YAML gateway configuration.",
    )
    parser.add_argument("--host", help="Override listen.host.")
    parser.add_argument("--port", type=int, help="Override listen.port.")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate the configuration and exit without serving.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="With --check-config, print the normalized config as JSON.",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Override logging.level.",
    )
    parser.add_argument(
        "--log-format",
        choices=["json", "text"],
        help="Override logging.format.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.host is not None or args.port is not None:
            config = replace(
                config,
                listen=replace(
                    config.listen,
                    host=args.host if args.host is not None else config.listen.host,
                    port=args.port if args.port is not None else config.listen.port,
                ),
            )
        configure_logging(
            level=args.log_level or config.logging.level,
            fmt=args.log_format or config.logging.format,
        )
        if args.check_config:
            return _check_config(config, as_json=args.json)
        return _serve(config)
    except GatewayError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"hint: {exc.hint}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 0


def _check_config(config: GatewayConfig, *, as_json: bool) -> int:
    summary = config.summary()
    if as_json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Configuration valid: {config.source_path}")
        print(
            f"Listen: {config.listen.host}:{config.listen.port} "
            "(port 0 chooses an ephemeral port)"
        )
        print(f"Backends: {len(config.backends)}")
        for backend in config.backends:
            print(
                f"  - {backend.name}: {backend.url} "
                f"(weight {backend.weight}, healthy path "
                f"{config.health.path})"
            )
        for warning in config.warnings:
            print(f"warning: {warning}", file=sys.stderr)
    return 0


def _serve(config: GatewayConfig) -> int:
    try:
        server = GatewayServer(config)
    except OSError as exc:
        raise OperationalError(
            f"could not bind {config.listen.host}:{config.listen.port}: {exc}",
            hint="Check whether another process is using the port.",
        ) from exc

    stop_event = threading.Event()

    def _stop(signum: int, frame: object) -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    address = f"http://{config.listen.host}:{server.port}"
    event(
        logger,
        logging.INFO,
        "gateway_started",
        "gateway listening",
        version=__version__,
        address=address,
        port=server.port,
        backends=[backend.name for backend in config.backends],
        health_path=config.health.path,
    )
    try:
        server.run(stop_event)
    finally:
        event(
            logger,
            logging.INFO,
            "gateway_stopped",
            "gateway stopped",
            address=address,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
