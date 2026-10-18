import time

from opsmender.db import Database
from opsmender.metrics.compute import compute
from opsmender.metrics.installer import install_script, probe_source
from opsmender.metrics.store import disk_forecast, history, rollup_and_prune, save_sample


def snap(ts, total, idle, usec=None, restarts=0):
    return {
        "v": 1, "ts": ts, "hostname": "vps", "uptime_s": 1000, "ncpu": 2,
        "cpu": {"total": total, "idle": idle}, "load": [0.5, 0.4, 0.3],
        "mem": {"total_kb": 4_000_000, "available_kb": 1_000_000, "swap_total_kb": 0, "swap_free_kb": 0},
        "disks": [{"mount": "/boot", "total_kb": 1000, "used_kb": 100, "avail_kb": 900},
                  {"mount": "/", "total_kb": 100_000, "used_kb": 80_000, "avail_kb": 20_000}],
        "docker": True,
        "containers": [{"id": "abc123", "name": "app-x1y2", "state": "running", "health": "", "restarts": restarts,
                        "oom_killed": False, "exit_code": 0, "started_at": "", "cpu_usec": usec,
                        "mem_bytes": 200 * 1_048_576, "mem_limit": None}],
    }


def test_compute_rates_between_snapshots():
    prev = snap(1000, 10_000, 8_000, usec=0)
    cur = snap(1060, 11_000, 8_250, usec=30_000_000)
    m = compute(prev, cur)
    assert round(m["cpu_pct"]) == 75          # 750 busy of 1000 jiffies
    assert round(m["mem_pct"]) == 75          # 3,000,000 of 4,000,000 kB used
    assert m["disk_used_pct"] == 80.0         # root mount chosen, not /boot
    assert round(m["containers"][0]["cpu_pct"]) == 50  # 30s of CPU over 60s


def test_first_snapshot_has_no_cpu_rate():
    m = compute(None, snap(1000, 10_000, 8_000, usec=0))
    assert m["cpu_pct"] is None
    assert m["containers"][0]["cpu_pct"] is None


def test_disk_forecast_detects_growth():
    day = 86400
    now = time.time()
    points = [(now - 6 * day + i * day, 50 + i * 5) for i in range(7)]  # +5 pts/day, now 80%
    f = disk_forecast(points)
    assert round(f["per_day"]) == 5
    assert round(f["days_left"]) == 4


def test_disk_forecast_flat_is_infinite():
    now = time.time()
    f = disk_forecast([(now - i * 3600, 40.0) for i in range(24, 0, -1)])
    assert f["days_left"] == float("inf")


def test_store_rollup_keeps_history(tmp_path):
    db = Database(tmp_path / "t.db")
    old = time.time() - 2 * 86400
    for i in range(30):
        m = compute(snap(old + i * 60, 0, 0), snap(old + (i + 1) * 60, 100, 50))
        save_sample(db, "srv", m)
    rollup_and_prune(db)
    assert db.scalar("SELECT COUNT(*) FROM metric_samples") == 0
    assert db.scalar("SELECT COUNT(*) FROM metric_rollups") >= 1
    points = history(db, "srv", hours=72)
    assert points and round(points[0]["cpu"]) == 50


def test_install_script_embeds_probe_and_restricts_key():
    script = install_script("ssh-ed25519 AAAATESTKEY opsmender")
    assert probe_source().strip().splitlines()[1] in script
    assert 'command="/usr/local/lib/opsmender/probe",restrict' in script
    assert "ssh-ed25519 AAAATESTKEY opsmender" in script


def test_install_script_rejects_odd_keys():
    import pytest
    for bad in ("not-a-key", "ssh-ed25519 AAA' ; rm -rf /", "ssh-ed25519 A\nssh-rsa B"):
        with pytest.raises(ValueError):
            install_script(bad)
