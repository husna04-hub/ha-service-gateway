"""Tiny controllable HTTP backend used by the demo, drill, and tests.

The control endpoints are intentionally local/demo-only and must never be
exposed on a public network.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


@dataclass
class BackendState:
    name: str
    fail: bool = False
    status_override: int | None = None
    delay_ms: int = 0
    requests: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "backend": self.name,
                "fail": self.fail,
                "status_override": self.status_override,
                "delay_ms": self.delay_ms,
                "requests": self.requests,
            }


class DemoHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: BackendState) -> None:
        self.state = state
        super().__init__(address, DemoHandler)


class DemoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ha-demo-backend/0.1"

    @property
    def state(self) -> BackendState:
        return self.server.state  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, *args: object) -> None:
        if os.environ.get("HA_DEMO_LOG"):
            super().log_message(*args)

    def _handle(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        query = parse_qs(parts.query)

        if path == "/healthz":
            with self.state.lock:
                fail = self.state.fail
            self._send_json(503 if fail else 200, {"backend": self.state.name, "ok": not fail})
            return

        if path == "/control/fail":
            with self.state.lock:
                if "state" in query:
                    self.state.fail = query["state"][0] not in {"0", "false", "no"}
                else:
                    self.state.fail = not self.state.fail
                fail = self.state.fail
            self._send_json(200, {"backend": self.state.name, "fail": fail})
            return

        if path == "/control/status":
            with self.state.lock:
                if "code" in query:
                    try:
                        code = int(query["code"][0])
                    except ValueError:
                        self._send_json(400, {"error": "code must be an integer"})
                        return
                    self.state.status_override = code if code else None
                value = self.state.status_override
            self._send_json(200, {"backend": self.state.name, "status": value})
            return

        if path == "/control/delay":
            with self.state.lock:
                try:
                    delay = int(query.get("ms", ["0"])[0])
                except ValueError:
                    self._send_json(400, {"error": "ms must be an integer"})
                    return
                self.state.delay_ms = max(0, min(delay, 60_000))
                value = self.state.delay_ms
            self._send_json(200, {"backend": self.state.name, "delay_ms": value})
            return

        if path == "/control/reset":
            with self.state.lock:
                self.state.fail = False
                self.state.status_override = None
                self.state.delay_ms = 0
                self.state.requests = 0
            self._send_json(200, {"backend": self.state.name, "reset": True})
            return

        body = self._read_body()
        with self.state.lock:
            self.state.requests += 1
            request_number = self.state.requests
            delay_ms = self.state.delay_ms
            override = self.state.status_override
        if delay_ms:
            time.sleep(delay_ms / 1000)
        payload = {
            "backend": self.state.name,
            "method": self.command,
            "path": path,
            "request": request_number,
            "body": body.decode("utf-8", errors="replace")[:4096],
        }
        self._send_json(override or 200, payload)
        if override:
            with self.state.lock:
                self.state.status_override = None

    def _read_body(self) -> bytes:
        raw = self.headers.get("Content-Length")
        if not raw:
            return b""
        try:
            length = int(raw)
        except ValueError:
            return b""
        if length < 0 or length > 1024 * 1024:
            return b""
        return self.rfile.read(length)

    def _send_json(
        self, status: int, payload: dict[str, object], *, head: bool = False
    ) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Demo-Backend", self.state.name)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if not head and self.command != "HEAD":
            self.wfile.write(body)


class DemoBackendServer:
    def __init__(
        self,
        name: str,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.state = BackendState(name=name)
        self.httpd = DemoHTTPServer((host, port), self.state)
        self.port = int(self.httpd.server_address[1])
        self.host = host
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "DemoBackendServer":
        self._thread = threading.Thread(
            target=self.httpd.serve_forever,
            name=f"demo-backend-{self.state.name}",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread is not None:
            self.httpd.shutdown()
            self._thread.join(timeout=2.0)
            self._thread = None
        self.httpd.server_close()

    def fail(self) -> None:
        with self.state.lock:
            self.state.fail = True

    def recover(self) -> None:
        with self.state.lock:
            self.state.fail = False

    def set_delay(self, milliseconds: int) -> None:
        with self.state.lock:
            self.state.delay_ms = milliseconds

    def set_status(self, code: int | None) -> None:
        with self.state.lock:
            self.state.status_override = code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ha-demo-backend",
        description="Controllable demo backend for the HA gateway.",
    )
    parser.add_argument(
        "--name",
        default=os.environ.get("BACKEND_NAME", "backend"),
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("BACKEND_HOST", "0.0.0.0"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "9001")),
    )
    args = parser.parse_args(argv)

    server = DemoBackendServer(args.name, args.host, args.port)
    print(
        f"demo backend {args.name} listening on http://{args.host}:{server.port}",
        flush=True,
    )
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
