# High-Availability HTTP Gateway

A dependency-light Python reverse proxy that routes traffic across multiple
backend instances, actively probes `/healthz`, removes unhealthy backends from
rotation, retries requests against another backend, and exposes Prometheus
metrics plus structured logs.

It is a deliberately small reliability prototype: the core failure-handling
logic is visible and testable without a service mesh, while the operational
surface (`/readyz`, `/metrics`, failover drill, Docker Compose) is realistic.

## Highlights

- **Active health checks** for every backend, with configurable interval,
  timeout, failure threshold, success threshold, and initial state.
- **Weighted smooth round-robin** selection across healthy backends. Health
  transitions reset selection weights so a recovered backend is not starved or
  flooded.
- **Failover on request failure**: connection errors and timeouts are retried
  against the next healthy backend; configured 5xx statuses are retried for
  idempotent methods. Each response reports `X-Backend` and
  `X-Gateway-Attempts`.
- **Readiness separate from liveness**: `/healthz` answers while the process is
  alive; `/readyz` returns 503 when no backend is healthy.
- **Prometheus metrics** for request status/latency, upstream failures, health
  checks and transitions, failovers, backend up/down, in-flight requests, and
  readiness.
- **Structured JSON logs** with request IDs, backend, attempts, status, latency,
  and health transitions.
- **Bounded request handling**: upstream timeout, attempt cap, request/response
  body caps, hop-by-hop header stripping, `X-Forwarded-*`, and request IDs.
- **Repeatable failure drill** that proves baseline routing, one-backend
  failover, recovery, total outage, and a concurrent load phase.
- **Docker Compose + Prometheus** assets for a complete local stack.

## Architecture

```text
                       +-----------------------------+
   client  ----------> |  ha-gateway (HTTP/1.1)      |
                       |  /healthz /readyz /metrics  |
                       |  /admin/backends            |
                       +--------------+--------------+
                                      |
                     weighted selection among healthy
                                      |
             +------------------------+------------------------+
             |                        |                        |
       +-----v-----+            +-----v-----+            +-----v-----+
       | backend-a |            | backend-b |            | backend-c |
       | /healthz  |            | /healthz  |            | /healthz  |
       +-----------+            +-----------+            +-----------+

   one health-prober thread per backend
   one threaded request handler per client connection
```

Health state machine:

```text
healthy --(failure_threshold consecutive failures)--> unhealthy
unhealthy --(success_threshold consecutive successes)--> healthy
```

## Quickstart

Linux/macOS:

```bash
cd ha-service-gateway
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"

pytest
python -m ha_gateway.drill --requests 300 --concurrency 32
```

Windows PowerShell:

```powershell
cd ha-service-gateway
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
python -m ha_gateway.drill --requests 300 --concurrency 32
```

The drill starts three controllable backends and the gateway on ephemeral
ports, then runs five phases:

```text
HA gateway failover drill
PHASE                  RESULT  DETAIL
baseline routing       PASS    distributed across 3 backends: {'alpha': 10, 'bravo': 10, 'charlie': 10}
one backend unhealthy  PASS    traffic shifted to {'bravo': 15, 'charlie': 15}
recovery               PASS    traffic returned to {'alpha': 10, 'bravo': 10, 'charlie': 10}
total outage           PASS    proxy=[503], readyz=503, healthz=200
load / recovery        PASS    300 requests, 0 failures, 413.3 req/s, p95 107.5 ms

Overall: PASS (300 requests, concurrency 32, 413.3 req/s, p95 107.5 ms)
```

The numbers are from one local run; throughput and latency depend on the host.
The drill exits non-zero if any phase fails, which makes it suitable for CI or a
pre-release gate.

## Run the stack with Docker

```bash
docker compose up --build -d
curl -s http://127.0.0.1:8080/healthz
curl -s http://127.0.0.1:8080/readyz
curl -s http://127.0.0.1:8080/admin/backends
curl -s http://127.0.0.1:8080/metrics | head
```

Open Prometheus at <http://127.0.0.1:9090> and query
`ha_gateway_backend_up` or `rate(ha_gateway_requests_total[1m])`.

To see failover in the Compose stack, stop one backend container and watch the
gateway adapt:

```bash
docker compose stop backend-a
for i in {1..12}; do
  curl -s -D - -o /dev/null http://127.0.0.1:8080/ | grep -i x-backend
  sleep 0.5
done
docker compose start backend-a
```

The first requests may still reach `backend-a` until its failure threshold is
crossed; after that the `X-Backend` header shows only `backend-b`/`backend-c`.

## Run manually

Start three demo backends and the gateway in separate terminals:

```bash
python -m ha_gateway.demo_backend --name api-1 --port 9001
python -m ha_gateway.demo_backend --name api-2 --port 9002
python -m ha_gateway.demo_backend --name api-3 --port 9003
python -m ha_gateway --config config/gateway.yaml
```

Then exercise the gateway:

```bash
curl -i http://127.0.0.1:8080/
curl -i http://127.0.0.1:8080/readyz
curl -s http://127.0.0.1:8080/admin/backends | python -m json.tool
```

The demo backend exposes `GET /control/fail?state=1|0`,
`/control/delay?ms=...`, and `/control/status?code=...` for local drills. These
control endpoints are **demo-only** and must not be exposed publicly.

## Endpoints

| Path | Behavior |
| --- | --- |
| `/healthz` | Liveness; `200` while the gateway process is running |
| `/readyz` | `200` when at least one backend is healthy, otherwise `503` |
| `/metrics` | Prometheus text exposition (path/configurable, can be disabled) |
| `/admin/backends` | Read-only JSON snapshot of backend health and counters |
| anything else | Proxied to the selected backend |

Successful proxied responses include:

- `X-Backend`: backend that served the response
- `X-Gateway-Attempts`: number of backend attempts
- `X-Request-Id`: propagated or generated request ID

## Configuration

See [config/gateway.yaml](config/gateway.yaml) and
[config/docker.yaml](config/docker.yaml).

```yaml
version: 1

listen:
  host: 0.0.0.0
  port: 8080

request:
  timeout_seconds: 2.0
  max_attempts: 3
  retry_on_status: [502, 503, 504]
  max_body_bytes: 10485760
  max_response_bytes: 10485760

health:
  path: /healthz
  interval_seconds: 2.0
  timeout_seconds: 0.8
  failure_threshold: 3
  success_threshold: 2
  initial_state: healthy   # or unhealthy for "probe before serving"

backends:
  - name: api-1
    url: http://127.0.0.1:9001
    weight: 1
  - name: api-2
    url: http://127.0.0.1:9002
    weight: 2

metrics:
  enabled: true
  path: /metrics

logging:
  level: INFO
  format: json
```

### Schema reference

| Section | Keys |
| --- | --- |
| Root | `version`, `listen`, `request`, `health`, `backends`, `metrics`, `logging` |
| `listen` | `host`, `port` (port `0` selects an ephemeral port) |
| `request` | `timeout_seconds`, `max_attempts`, `retry_on_status`, `max_body_bytes`, `max_response_bytes` |
| `health` | `path`, `interval_seconds`, `timeout_seconds`, `failure_threshold`, `success_threshold`, `initial_state` |
| `backends[]` | `name`, `url`, `weight` (1-100) |
| `metrics` | `enabled`, `path` |
| `logging` | `level`, `format` (`json` or `text`) |

Validation is strict: unknown keys, duplicate backend names, invalid URLs,
credentials in URLs, invalid thresholds, metrics/health path collisions, and
out-of-range values fail with a path and a hint before the socket is bound.

## Failover semantics

1. The loader picks a backend using smooth weighted round-robin among currently
   healthy backends, excluding backends already attempted for this request.
2. The gateway connects with `request.timeout_seconds`, strips hop-by-hop
   headers, adds forwarding/request-ID headers, and sends the body.
3. A transport error or timeout records an upstream failure and tries the next
   eligible backend, up to `request.max_attempts`.
4. A response in `request.retry_on_status` is retried only for idempotent
   methods (`GET`, `HEAD`, `OPTIONS`, `PUT`, `DELETE`, `TRACE`), up to the
   attempt cap.
5. If no backend is healthy before the first attempt, the gateway returns
   `503` with `Retry-After: 1`; if attempts were made and all failed, it returns
   the last `502`/`504` (or retryable upstream status) with a JSON error.
6. Active health checks run independently and move a backend out of (or back
   into) rotation after the configured consecutive thresholds.

`initial_state: unhealthy` is the safer choice when the gateway must not send
traffic before the first successful probe; the default `healthy` favors fast
startup.

## Metrics

| Metric | Labels | Meaning |
| --- | --- | --- |
| `ha_gateway_requests_total` | `method`, `status`, `backend` | Final gateway responses |
| `ha_gateway_request_duration_seconds` | `backend` | End-to-end latency histogram |
| `ha_gateway_upstream_failures_total` | `backend`, `reason` | Transport, timeout, or retryable status failures |
| `ha_gateway_health_checks_total` | `backend`, `result` | Active probe outcomes |
| `ha_gateway_backend_health_transitions_total` | `backend`, `state` | Healthy/unhealthy transitions |
| `ha_gateway_failovers_total` | `from_backend`, `to_backend` | Requests that moved to another backend |
| `ha_gateway_backend_up` | `backend` | Current health (1/0) |
| `ha_gateway_inflight_requests` | — | In-flight requests |
| `ha_gateway_ready` | — | Readiness (1/0) |

A representative exposition is checked in at
[examples/metrics.example.txt](examples/metrics.example.txt).

## Testing

The suite has **54 tests** covering:

- config loading, defaults, strict validation, URL normalization, and warnings;
- health state transitions, probe success/failure, and prober lifecycles;
- weighted round-robin distribution, exclusions, unhealthy skipping, readiness,
  and snapshots;
- Prometheus counters, gauges, histograms, label escaping;
- proxy routing, failover on health loss, timeout failover, 5xx retry, POST body
  forwarding, request IDs, HEAD, metrics, total outage, and 120 concurrent
  requests;
- the full five-phase failover drill as an end-to-end test;
- CLI config validation and overrides.

```bash
pytest -q
```

## Load testing

With the gateway running:

```bash
python scripts/load_test.py --url http://127.0.0.1:8080 \
  --requests 1000 --concurrency 32
```

The script reports throughput, status counts, and p50/p95/p99 latency, and
exits non-zero if any request fails.

## Project layout

```text
ha-service-gateway/
  config/                  local and Docker gateway configs
  deploy/prometheus.yml    scrape configuration
  scripts/load_test.py     standalone load generator
  src/ha_gateway/
    config.py              strict schema and YAML loader
    health.py              probe + health state machine
    gateway.py             weighted selection, proxy handler, server
    metrics.py             dependency-free Prometheus exposition
    demo_backend.py        controllable backends for demo/drill/tests
    drill.py               five-phase failover + load drill
    cli.py                 `ha-gateway` entry point
    log.py                 structured logging
  tests/                   54 pytest tests
  Dockerfile
  docker-compose.yml
```

## Resume bullet mapping

| Claim | Implementation |
| --- | --- |
| Service-routing prototype with multiple backends, health checks, timeouts, failover | `GatewayApp`, weighted selection, active probers, retry loop in `gateway.py` |
| Service metrics and structured logs for latency, availability, error rates, backend health | Prometheus families in `metrics.py`; request/health fields in `log.py` |
| Evaluated failover behavior under controlled backend failures with repeatable integration and load tests | `drill.py` five-phase drill, `tests/test_gateway.py`, `tests/test_drill.py`, `scripts/load_test.py` |

## Production limits

This is a portfolio-grade prototype, not a replacement for Envoy/HAProxy/NGINX:

- One upstream connection per request; no pooling or HTTP/2.
- Chunked request bodies are rejected with `501`; request/response bodies are
  buffered in memory up to configured caps.
- The listener itself is HTTP; TLS termination is expected in front of it.
- Transport failures are retried even for non-idempotent methods; production
  traffic should use idempotency keys or disable retries for unsafe methods.
- No authentication/authorization on admin or metrics endpoints; keep them on a
  private network.
- Health checks verify HTTP status, not business-level correctness.
- The demo backend's `/control/*` endpoints are intentionally insecure and
  should never be deployed publicly.
