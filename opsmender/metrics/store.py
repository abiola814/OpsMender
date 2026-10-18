"""Persist computed samples, roll them up, and forecast disk usage."""

from __future__ import annotations

import time
from typing import Any

from ..db import Database

RAW_RETENTION = 24 * 3600
ROLLUP_RETENTION = 30 * 24 * 3600
ROLLUP_BUCKET = 15 * 60


def save_sample(db: Database, server_uuid: str, m: dict[str, Any]) -> None:
    ts = m.get("ts") or time.time()
    db.execute(
        "INSERT INTO metric_samples (server_uuid, ts, cpu_pct, load1, mem_used, mem_total, swap_used,"
        " disk_used_pct, disk_used, disk_total) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (server_uuid, ts, m.get("cpu_pct"), m.get("load1"), m.get("mem_used"), m.get("mem_total"),
         m.get("swap_used"), m.get("disk_used_pct"), m.get("disk_used"), m.get("disk_total")),
    )
    rows = [(server_uuid, ts, c["name"], c.get("cpu_pct"), c.get("mem_bytes"), c.get("restarts"))
            for c in m.get("containers", []) if c.get("state") == "running" or c.get("restarts")]
    if rows:
        db.executemany(
            "INSERT INTO container_samples (server_uuid, ts, name, cpu_pct, mem_bytes, restarts) VALUES (?,?,?,?,?,?)",
            rows,
        )


def recent_samples(db: Database, server_uuid: str, seconds: int = 3600) -> list[dict[str, Any]]:
    return db.all(
        "SELECT * FROM metric_samples WHERE server_uuid = ? AND ts >= ? ORDER BY ts",
        (server_uuid, time.time() - seconds),
    )


def history(db: Database, server_uuid: str, hours: int = 24) -> list[dict[str, Any]]:
    """Per-minute samples for up to 24h, rollups beyond that, as a uniform series."""
    since = time.time() - hours * 3600
    points = []
    if hours > 24:
        for r in db.all("SELECT * FROM metric_rollups WHERE server_uuid = ? AND bucket_ts >= ? ORDER BY bucket_ts",
                        (server_uuid, since)):
            points.append({"ts": r["bucket_ts"], "cpu": r["cpu_avg"], "mem": r["mem_pct_avg"], "disk": r["disk_used_pct"]})
    for r in db.all("SELECT * FROM metric_samples WHERE server_uuid = ? AND ts >= ? ORDER BY ts", (server_uuid, since)):
        mem = 100.0 * r["mem_used"] / r["mem_total"] if r["mem_total"] else None
        points.append({"ts": r["ts"], "cpu": r["cpu_pct"], "mem": mem, "disk": r["disk_used_pct"]})
    return points


def container_history(db: Database, server_uuid: str, name: str, hours: int = 6) -> list[dict[str, Any]]:
    return db.all(
        "SELECT ts, cpu_pct, mem_bytes, restarts FROM container_samples WHERE server_uuid = ? AND name = ? AND ts >= ?"
        " ORDER BY ts", (server_uuid, name, time.time() - hours * 3600),
    )


def rollup_and_prune(db: Database) -> None:
    now = time.time()
    # Align the cutoff to a bucket boundary so each bucket is rolled up exactly once, complete.
    cutoff = int((now - RAW_RETENTION) // ROLLUP_BUCKET) * ROLLUP_BUCKET
    db.execute(
        f"""INSERT OR REPLACE INTO metric_rollups (server_uuid, bucket_ts, cpu_avg, cpu_max, mem_pct_avg, mem_pct_max, disk_used_pct)
        SELECT server_uuid, CAST(ts / {ROLLUP_BUCKET} AS INTEGER) * {ROLLUP_BUCKET} AS bucket,
               AVG(cpu_pct), MAX(cpu_pct),
               AVG(CASE WHEN mem_total > 0 THEN 100.0 * mem_used / mem_total END),
               MAX(CASE WHEN mem_total > 0 THEN 100.0 * mem_used / mem_total END),
               AVG(disk_used_pct)
        FROM metric_samples WHERE ts < ? GROUP BY server_uuid, bucket""",
        (cutoff,),
    )
    db.execute("DELETE FROM metric_samples WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM container_samples WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM metric_rollups WHERE bucket_ts < ?", (now - ROLLUP_RETENTION,))
    db.execute("DELETE FROM events WHERE ts < ?", (now - ROLLUP_RETENTION,))


def disk_forecast(points: list[tuple[float, float]]) -> dict[str, float] | None:
    """Least-squares trend of (ts, used_pct). Returns growth per day and days until 100%."""
    points = [(t, v) for t, v in points if v is not None]
    if len(points) < 6:
        return None
    span = points[-1][0] - points[0][0]
    if span < 6 * 3600:
        return None
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_v = sum(v for _, v in points) / n
    var = sum((t - mean_t) ** 2 for t, _ in points)
    if var == 0:
        return None
    slope = sum((t - mean_t) * (v - mean_v) for t, v in points) / var  # pct per second
    per_day = slope * 86400
    current = points[-1][1]
    if per_day <= 0.05:
        return {"per_day": per_day, "days_left": float("inf"), "current": current}
    return {"per_day": per_day, "days_left": max(0.0, (100.0 - current) / per_day), "current": current}


def disk_series(db: Database, server_uuid: str, days: int = 7) -> list[tuple[float, float]]:
    since = time.time() - days * 86400
    series = [(r["bucket_ts"], r["disk_used_pct"]) for r in db.all(
        "SELECT bucket_ts, disk_used_pct FROM metric_rollups WHERE server_uuid = ? AND bucket_ts >= ? ORDER BY bucket_ts",
        (server_uuid, since))]
    series += [(r["ts"], r["disk_used_pct"]) for r in db.all(
        "SELECT ts, disk_used_pct FROM metric_samples WHERE server_uuid = ? AND ts >= ? ORDER BY ts",
        (server_uuid, since))]
    return series
