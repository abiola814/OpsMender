"""Right-sizing advice from container history: limits, leaks, idle apps, busy apps."""
import time
from datetime import datetime, timezone

from opsmender import advice
from opsmender.db import dumps
from opsmender.metrics.store import rollup_and_prune

MB = 1_048_576
HOUR = 3600
APP = "app1-123456789012"   # Coolify application container: <uuid>-<deploy timestamp>


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def container(name, mem, limit=None, started=None, oom=False):
    return {"id": name[:12], "name": name, "state": "running", "health": None, "restarts": 0, "oom_killed": oom,
            "exit_code": 0, "started_at": iso(started or time.time() - 30 * 86400), "mem_bytes": mem,
            "mem_limit": limit, "cpu_pct": 1.0}


def setup_server(rt, containers, mem_total=4096 * MB, mem_used=1024 * MB):
    rt.db.execute("INSERT INTO servers (uuid, name, metrics_enabled, current_metrics) VALUES ('srv1', 'main', 1, ?)",
                  (dumps({"mem_total": mem_total, "mem_used": mem_used, "mem_pct": 100 * mem_used / mem_total,
                          "ncpu": 2, "containers": containers}),))
    for uuid, kind, name in (("app1", "application", "api"), ("db1", "database", "postgres"),
                             ("svc1", "service", "plausible")):
        rt.db.execute("INSERT INTO resources (uuid, kind, name, server_uuid) VALUES (?,?,?, 'srv1')", (uuid, kind, name))


def samples(rt, name, start, hours, mem, cpu=1.0, step=600):
    """Per-sample rows from `start` for `hours`; mem may be a function of the hour offset."""
    rows = []
    t = start
    while t < start + hours * HOUR:
        m = mem((t - start) / HOUR) if callable(mem) else mem
        rows.append(("srv1", t, name, cpu, int(m), 0))
        t += step
    rt.db.executemany("INSERT INTO container_samples (server_uuid, ts, name, cpu_pct, mem_bytes, restarts)"
                      " VALUES (?,?,?,?,?,?)", rows)


def kinds(rt):
    return {(a["kind"], (a["resource"] or {}).get("name")): a for a in advice.all_advice(rt)}


def test_near_limit_suggests_a_bigger_limit_that_can_be_applied(rt):
    setup_server(rt, [container(APP, 470 * MB, limit=512 * MB)])
    samples(rt, APP, time.time() - 3 * HOUR, 3, 480 * MB)
    a = kinds(rt)[("mem_near_limit", "api")]
    assert a["severity"] == "warning"
    assert a["limit"] == "640m"          # 480 MB peak x 1.3, rounded up to 64 MB
    assert a["can_apply"]
    assert "Resource Limits" in a["action"]


def test_no_limit_needs_a_day_of_history_and_counts_old_deploys(rt):
    setup_server(rt, [container(APP, 200 * MB)])
    samples(rt, APP, time.time() - 2 * HOUR, 2, 200 * MB)
    assert ("mem_no_limit", "api") not in kinds(rt)   # not enough history yet

    # The previous deployment (different container name, same app) peaked higher two days ago.
    samples(rt, "app1-111111111111", time.time() - 50 * HOUR, 5, 400 * MB)
    a = kinds(rt)[("mem_no_limit", "api")]
    assert a["mem_peak"] == 400 * MB
    assert a["limit"] == "640m"          # 400 MB x 1.5 = 600 MB, rounded up


def test_service_limit_points_at_the_compose_service(rt):
    name = "clickhouse-svc1"
    setup_server(rt, [container(name, 300 * MB)])
    samples(rt, name, time.time() - 30 * HOUR, 30, 300 * MB)
    a = kinds(rt)[("mem_no_limit", "plausible")]
    assert "plausible (clickhouse)" in a["title"]
    assert "`mem_limit: 512m`" in a["action"] and "`clickhouse` service" in a["action"]
    assert not a["can_apply"]            # Coolify's API can't change a service's compose limits


def test_leak_is_detected_and_alerts_before_the_kill(rt):
    started = time.time() - 12 * HOUR
    setup_server(rt, [container(APP, 900 * MB, limit=1024 * MB, started=started)])
    samples(rt, APP, started + HOUR, 11, lambda h: 350 * MB + h * 50 * MB)  # +50 MB an hour, steadily
    a = kinds(rt)[("mem_leak", "api")]
    assert 40 * MB < a["growth_per_hour"] < 60 * MB
    assert a["hours_left"] < 24

    advice.check_leaks(rt)
    inc = rt.db.one("SELECT * FROM incidents WHERE kind = 'memory_leak' AND status = 'open'")
    assert inc and inc["resource_uuid"] == "app1"

    rt.db.execute("DELETE FROM container_samples")
    samples(rt, APP, started + HOUR, 11, 600 * MB)  # flat
    advice.check_leaks(rt)
    assert rt.db.one("SELECT status FROM incidents WHERE id = ?", (inc["id"],))["status"] == "resolved"


def test_noisy_memory_is_not_a_leak(rt):
    started = time.time() - 12 * HOUR
    setup_server(rt, [container(APP, 400 * MB, started=started)])
    samples(rt, APP, started + HOUR, 11, lambda h: 400 * MB + (150 * MB if int(h) % 2 else -150 * MB))
    assert ("mem_leak", "api") not in kinds(rt)


def test_idle_app_and_busy_app(rt):
    setup_server(rt, [container(APP, 300 * MB), container("db1", 500 * MB)])
    samples(rt, APP, time.time() - 4 * 86400, 96, 300 * MB, cpu=0.05, step=1800)
    samples(rt, "db1", time.time() - 8 * HOUR, 8, 500 * MB, cpu=120.0)
    found = kinds(rt)
    assert found[("idle", "api")]["saving"] == 300 * MB
    assert found[("cpu_heavy", "postgres")]["severity"] == "warning"
    assert ("idle", "postgres") not in found     # databases are never called idle


def test_rollups_keep_two_weeks_of_container_history(rt):
    setup_server(rt, [container(APP, 200 * MB)])
    samples(rt, APP, time.time() - 3 * 86400, 30, 250 * MB)    # older than the 24h raw window
    before = advice.all_advice(rt)
    rollup_and_prune(rt.db)
    assert rt.db.scalar("SELECT COUNT(*) FROM container_samples WHERE ts < ?", (time.time() - 2 * 86400,)) == 0
    assert 30 <= rt.db.scalar("SELECT COUNT(*) FROM container_rollups") <= 31   # hourly buckets
    after = advice.all_advice(rt)
    assert [a["mem_peak"] for a in before] == [a["mem_peak"] for a in after] == [250 * MB]


def test_suggested_limits_are_tidy():
    assert advice.docker_size(advice.suggest_limit(100 * MB, 1.5)) == "192m"   # at least peak + 64 MB
    assert advice.docker_size(advice.suggest_limit(10 * MB, 1.5)) == "128m"
    assert advice.docker_size(advice.suggest_limit(1800 * MB, 1.3)) == "2560m"
    assert advice.docker_size(advice.suggest_limit(1500 * MB, 1.3)) == "2g"
    assert advice.valid_limit("512m") and advice.valid_limit("2g")
    assert not advice.valid_limit("512") and not advice.valid_limit("0m") and not advice.valid_limit("1g; rm")
