from __future__ import annotations

from ha_gateway.drill import run_drill


def test_failover_drill_end_to_end() -> None:
    report = run_drill(requests=60, concurrency=12)
    assert report.ok, report.to_dict()
    names = [phase.name for phase in report.phases]
    assert names == [
        "baseline routing",
        "one backend unhealthy",
        "recovery",
        "total outage",
        "load / recovery",
    ]
    assert report.load_rps > 0
    assert report.load_p95_ms >= 0
