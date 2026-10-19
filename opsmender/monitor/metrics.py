"""Poll each metrics-enabled server through the SSH probe and raise threshold alerts."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..db import dumps, loads
from ..incidents import open_fingerprints, open_incident, resolve_incident
from ..metrics import ssh
from ..metrics.compute import compute
from ..metrics.store import container_history, disk_forecast, disk_series, recent_samples, save_sample
from ..runtime import Runtime

log = logging.getLogger("opsmender.metrics")
PROBE_FAILURES_BEFORE_ALERT = 3


async def poll_all(rt: Runtime) -> None:
    key = rt.settings.get("ssh_private_key")
    if not key:
        return
    servers = rt.db.all("SELECT * FROM servers WHERE metrics_enabled = 1")
    if not servers:
        return
    sem = asyncio.Semaphore(4)

    async def one(server: dict[str, Any]) -> None:
        async with sem:
            await poll_server(rt, server, key)

    await asyncio.gather(*(one(s) for s in servers))
    rt.status["last_metrics_at"] = time.time()


async def poll_server(rt: Runtime, server: dict[str, Any], key: str) -> dict[str, Any] | None:
    host = server["probe_host"] or server["ip"]
    port = server["probe_port"] or 22
    try:
        host_key, raw = await ssh.snapshot(host, port, key, server["host_key"])
    except ssh.ProbeError as exc:
        failures = (server["probe_failures"] or 0) + 1
        rt.db.execute("UPDATE servers SET last_probe_error = ?, probe_failures = ? WHERE uuid = ?",
                      (str(exc), failures, server["uuid"]))
        if failures == PROBE_FAILURES_BEFORE_ALERT:
            open_incident(rt, fingerprint=f"probe:{server['uuid']}", kind="metrics_unavailable", severity="warning",
                          title=f"Cannot collect metrics from {server['name']}", summary=str(exc),
                          server_uuid=server["uuid"], enrich_now=False)
        return None

    prev = loads(server.get("last_snapshot"))
    current = compute(prev, raw)
    rt.db.execute(
        "UPDATE servers SET host_key = COALESCE(host_key, ?), last_probe_at = ?, last_probe_error = NULL,"
        " probe_failures = 0, last_snapshot = ?, current_metrics = ? WHERE uuid = ?",
        (host_key, time.time(), dumps(raw), dumps(current), server["uuid"]),
    )
    resolve_incident(rt, f"probe:{server['uuid']}", "metrics are flowing again")
    if prev is not None:  # the first snapshot has no CPU rate yet
        save_sample(rt.db, server["uuid"], current)
        evaluate_server(rt, server, current, prev)
    return current


def evaluate_server(rt: Runtime, server: dict[str, Any], current: dict[str, Any], prev_raw: dict[str, Any]) -> None:
    uuid, name = server["uuid"], server["name"]
    sustain = max(1, rt.settings.int("sustain_samples"))
    samples = recent_samples(rt.db, uuid, seconds=sustain * rt.settings.int("metrics_interval") * 2 + 60)[-sustain:]

    def sustained(values: list[float | None], threshold: float) -> bool:
        return len(values) >= sustain and all(v is not None and v >= threshold for v in values)

    cpu_t = rt.settings.int("cpu_threshold")
    cpu_values = [s["cpu_pct"] for s in samples]
    if sustained(cpu_values, cpu_t):
        open_incident(rt, fingerprint=f"cpu:{uuid}", kind="high_cpu", severity="warning",
                      title=f"High CPU on {name}", server_uuid=uuid, enrich_now=False,
                      summary=f"CPU above {cpu_t}% for {sustain} consecutive samples (now {cpu_values[-1]:.0f}%).",
                      evidence={"metrics": _metric_evidence(current), "top_containers": _top(current, "cpu_pct")})
    elif current.get("cpu_pct") is not None and current["cpu_pct"] < cpu_t - 10:
        resolve_incident(rt, f"cpu:{uuid}", f"CPU back to {current['cpu_pct']:.0f}%")

    mem_t = rt.settings.int("mem_threshold")
    mem_values = [100.0 * s["mem_used"] / s["mem_total"] if s["mem_total"] else None for s in samples]
    if sustained(mem_values, mem_t):
        open_incident(rt, fingerprint=f"mem:{uuid}", kind="high_memory", severity="warning",
                      title=f"High memory usage on {name}", server_uuid=uuid, enrich_now=False,
                      summary=f"Memory above {mem_t}% for {sustain} consecutive samples (now {mem_values[-1]:.0f}%).",
                      evidence={"metrics": _metric_evidence(current), "top_containers": _top(current, "mem_bytes")})
    elif current.get("mem_pct") is not None and current["mem_pct"] < mem_t - 5:
        resolve_incident(rt, f"mem:{uuid}", f"memory back to {current['mem_pct']:.0f}%")

    disk_t = rt.settings.int("disk_threshold")
    disk = current.get("disk_used_pct")
    if disk is not None and disk >= disk_t:
        open_incident(rt, fingerprint=f"disk:{uuid}", kind="disk_pressure",
                      severity="critical" if disk >= 95 else "warning",
                      title=f"Disk {disk:.0f}% full on {name}", server_uuid=uuid,
                      summary="OpsMender is checking what is using the space (read-only).",
                      evidence={"metrics": _metric_evidence(current), "disks": current.get("disks")})
    elif disk is not None and disk < disk_t - 2:
        resolve_incident(rt, f"disk:{uuid}", f"disk at {disk:.0f}%")

    _evaluate_containers(rt, server, current, prev_raw)


def _evaluate_containers(rt: Runtime, server: dict[str, Any], current: dict[str, Any], prev_raw: dict[str, Any]) -> None:
    uuid, name = server["uuid"], server["name"]
    before = {c.get("name"): c for c in prev_raw.get("containers") or []}
    restart_threshold = max(1, rt.settings.int("restart_threshold"))
    still_looping: set[str] = set()
    for c in current.get("containers", []):
        cname = c.get("name") or ""
        prev = before.get(cname)
        resource = _resource_for_container(rt, cname)
        label = resource["name"] if resource else cname
        if c.get("oom_killed") and prev is not None and not prev.get("oom_killed"):
            open_incident(rt, fingerprint=f"oom:{uuid}:{cname}", kind="out_of_memory", severity="critical",
                          title=f"{label} was killed for running out of memory", resource=resource, server_uuid=uuid,
                          summary=f"Container {cname} on {name} was OOM-killed "
                                  f"(limit {_mb(c.get('mem_limit'))}).",
                          evidence={"container": _container_evidence(c)})
        history = container_history(rt.db, uuid, cname, hours=1)
        window = [h for h in history if h["ts"] >= time.time() - 600 and h["restarts"] is not None]
        if len(window) >= 2:
            restarts = window[-1]["restarts"] - window[0]["restarts"]
            if restarts >= restart_threshold:
                still_looping.add(cname)
                open_incident(rt, fingerprint=f"crash:{uuid}:{cname}", kind="crash_loop", severity="critical",
                              title=f"{label} is crash-looping", resource=resource, server_uuid=uuid,
                              summary=f"{restarts} restarts in the last 10 minutes on {name}.",
                              evidence={"container": _container_evidence(c)})
    for fp in open_fingerprints(rt, f"crash:{uuid}:"):
        if fp.split(":", 2)[2] not in still_looping:
            resolve_incident(rt, fp, "no restarts in the last 10 minutes")


def check_disk_forecasts(rt: Runtime) -> None:
    horizon = rt.settings.int("disk_forecast_days")
    for server in rt.db.all("SELECT uuid, name FROM servers WHERE metrics_enabled = 1"):
        forecast = disk_forecast(disk_series(rt.db, server["uuid"]))
        fp = f"diskforecast:{server['uuid']}"
        if forecast and forecast["days_left"] <= horizon:
            open_incident(rt, fingerprint=fp, kind="disk_forecast", severity="warning",
                          title=f"Disk on {server['name']} will be full in ~{forecast['days_left']:.1f} days",
                          summary=f"Growing {forecast['per_day']:.2f} percentage points per day, "
                                  f"now {forecast['current']:.0f}% used.",
                          server_uuid=server["uuid"], evidence={"forecast": forecast})
        elif forecast is None or forecast["days_left"] > horizon * 2:
            resolve_incident(rt, fp, "disk growth slowed")


def _resource_for_container(rt: Runtime, container_name: str) -> dict[str, Any] | None:
    for r in rt.db.all("SELECT uuid, kind, name, server_uuid FROM resources WHERE missing = 0"):
        if r["uuid"] and r["uuid"] in container_name:
            return r
    return None


def _metric_evidence(m: dict[str, Any]) -> dict[str, Any]:
    return {k: m.get(k) for k in ("cpu_pct", "mem_pct", "disk_used_pct", "load1")}


def _container_evidence(c: dict[str, Any]) -> dict[str, Any]:
    out = {k: c.get(k) for k in ("name", "state", "health", "restarts", "oom_killed", "exit_code", "cpu_pct")}
    out["mem_mb"] = round(c["mem_bytes"] / 1_048_576) if c.get("mem_bytes") else None
    out["mem_limit_mb"] = round(c["mem_limit"] / 1_048_576) if c.get("mem_limit") else None
    return out


def _top(m: dict[str, Any], key: str, n: int = 5) -> list[dict[str, Any]]:
    running = [c for c in m.get("containers", []) if c.get("state") == "running" and c.get(key) is not None]
    return [_container_evidence(c) for c in sorted(running, key=lambda c: c[key], reverse=True)[:n]]


def _mb(value: Any) -> str:
    return f"{round(value / 1_048_576)} MB" if value else "no limit"
