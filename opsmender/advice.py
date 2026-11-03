"""Right-sizing advice: memory, CPU and disk on each server that are at risk or going to waste.

Read-only. Everything comes from data OpsMender already has: per-minute container samples (24h),
hourly container rollups (14 days), the latest probe snapshot, the server's memory history and the
last disk report. Advice is computed on demand; only a predicted out-of-memory opens an incident.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime
from typing import Any

from .db import loads
from .incidents import open_fingerprints, open_incident, resolve_incident
from .metrics import disk
from .runtime import Runtime

MB = 1_048_576
GB = 1024 * MB
HOUR = 3600
DAY = 86400
WINDOW = 7 * DAY
# Coolify names application containers <uuid>-<HHMMSSffffff>, a new name on every deployment.
_DEPLOY_SUFFIX = re.compile(r"-\d{12}$")
_LIMIT_VALUE = re.compile(r"^[1-9]\d{0,6}[mg]$")
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
CATEGORY_LABELS = {"memory": "Memory", "cpu": "CPU", "disk": "Disk", "server": "Server size"}
LIMIT_KINDS = {"application", "database"}  # kinds whose memory limit Coolify's API can change
LEAK_ALERT_HOURS = 24


def identity(container_name: str) -> str:
    """The same app across redeploys: strip Coolify's per-deployment suffix."""
    return _DEPLOY_SUFFIX.sub("", container_name or "")


def human(n: float | None) -> str:
    if n is None:
        return "–"
    return f"{n / GB:.1f} GB" if abs(n) >= GB else f"{n / MB:.0f} MB"


def docker_size(n: int) -> str:
    return f"{n // GB}g" if n % GB == 0 else f"{math.ceil(n / MB)}m"


def valid_limit(value: str) -> bool:
    return bool(_LIMIT_VALUE.match(value or ""))


def suggest_limit(peak: float, factor: float) -> int:
    """A memory limit with headroom over the observed peak, rounded to a tidy size."""
    target = max(peak * factor, peak + 64 * MB, 128 * MB)
    step = 64 * MB if target < GB else 256 * MB
    return int(math.ceil(target / step) * step)


# Data

def _container_usage(rt: Runtime, server_uuid: str, since: float) -> dict[str, dict[str, Any]]:
    """Per container identity: CPU and memory averages and peaks over raw samples plus hourly rollups."""
    rows = rt.db.all(
        "SELECT name, COUNT(cpu_pct) AS cpu_n, SUM(cpu_pct) AS cpu_sum, MAX(cpu_pct) AS cpu_max,"
        " COUNT(mem_bytes) AS mem_n, SUM(mem_bytes) AS mem_sum, MAX(mem_bytes) AS mem_max,"
        " MIN(ts) AS first, MAX(ts) AS last FROM container_samples WHERE server_uuid = ? AND ts >= ? GROUP BY name",
        (server_uuid, since))
    rows += rt.db.all(
        "SELECT name, SUM(CASE WHEN cpu_avg IS NOT NULL THEN samples END) AS cpu_n, SUM(cpu_avg * samples) AS cpu_sum,"
        " MAX(cpu_max) AS cpu_max, SUM(CASE WHEN mem_avg IS NOT NULL THEN samples END) AS mem_n,"
        " SUM(mem_avg * samples) AS mem_sum, MAX(mem_max) AS mem_max, MIN(bucket_ts) AS first,"
        f" MAX(bucket_ts) + {HOUR} AS last FROM container_rollups WHERE server_uuid = ? AND bucket_ts >= ? GROUP BY name",
        (server_uuid, since))
    usage: dict[str, dict[str, Any]] = {}
    for r in rows:
        u = usage.setdefault(identity(r["name"]), {"cpu_n": 0, "cpu_sum": 0.0, "cpu_max": None, "mem_n": 0,
                                                   "mem_sum": 0.0, "mem_max": None, "first": None, "last": None})
        u["cpu_n"] += r["cpu_n"] or 0
        u["cpu_sum"] += r["cpu_sum"] or 0.0
        u["mem_n"] += r["mem_n"] or 0
        u["mem_sum"] += r["mem_sum"] or 0.0
        for key, pick in (("cpu_max", max), ("mem_max", max), ("first", min), ("last", max)):
            if r[key] is not None:
                u[key] = r[key] if u[key] is None else pick(u[key], r[key])
    for u in usage.values():
        u["cpu_avg"] = u["cpu_sum"] / u["cpu_n"] if u["cpu_n"] else None
        u["mem_avg"] = u["mem_sum"] / u["mem_n"] if u["mem_n"] else None
        u["coverage"] = (u["last"] - u["first"]) if u["first"] is not None else 0
    return usage


def _hourly_memory(rt: Runtime, server_uuid: str, name: str, since: float) -> list[tuple[float, float]]:
    points = [(r["bucket_ts"] + HOUR / 2, r["mem_avg"]) for r in rt.db.all(
        "SELECT bucket_ts, mem_avg FROM container_rollups WHERE server_uuid = ? AND name = ? AND bucket_ts >= ?"
        " AND mem_avg IS NOT NULL", (server_uuid, name, since))]
    points += [(r["b"] + HOUR / 2, r["m"]) for r in rt.db.all(
        f"SELECT CAST(ts / {HOUR} AS INTEGER) * {HOUR} AS b, AVG(mem_bytes) AS m FROM container_samples"
        " WHERE server_uuid = ? AND name = ? AND ts >= ? AND mem_bytes IS NOT NULL GROUP BY b",
        (server_uuid, name, since))]
    return sorted(points)


def _host_memory(rt: Runtime, server_uuid: str) -> dict[str, Any]:
    """Server memory over the last day (per-minute) and the last week (15-minute rollups)."""
    day = [100.0 * r["mem_used"] / r["mem_total"] for r in rt.db.all(
        "SELECT mem_used, mem_total FROM metric_samples WHERE server_uuid = ? AND ts >= ? AND mem_total > 0",
        (server_uuid, time.time() - DAY))]
    week = rt.db.one(
        "SELECT COUNT(*) AS n, MIN(bucket_ts) AS first, MAX(mem_pct_max) AS mem_max, AVG(cpu_avg) AS cpu_avg,"
        " MAX(cpu_max) AS cpu_max FROM metric_rollups WHERE server_uuid = ? AND bucket_ts >= ?",
        (server_uuid, time.time() - WINDOW)) or {}
    day.sort()
    return {"median_day": day[len(day) // 2] if day else None, "peak_day": day[-1] if day else None,
            "week_span": time.time() - week["first"] if week.get("first") else 0,
            "week_mem_max": week.get("mem_max"), "week_cpu_avg": week.get("cpu_avg"), "week_cpu_max": week.get("cpu_max")}


def _trend(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Least-squares slope (bytes per hour) and R² of a memory series."""
    n = len(points)
    if n < 6:
        return None
    mean_t = sum(t for t, _ in points) / n
    mean_v = sum(v for _, v in points) / n
    var_t = sum((t - mean_t) ** 2 for t, _ in points)
    var_v = sum((v - mean_v) ** 2 for _, v in points)
    if not var_t or not var_v:
        return None
    cov = sum((t - mean_t) * (v - mean_v) for t, v in points)
    return cov / var_t * HOUR, cov * cov / (var_t * var_v)


def _started(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


# Advice

def _where_to_set(resource: dict[str, Any], container: str, value: str) -> str:
    if resource["kind"] == "service":
        # Service containers are named <compose service>-<service uuid>.
        sub = container.split("-" + resource["uuid"])[0]
        return (f"Coolify services take limits from their compose file: add `mem_limit: {value}` to the "
                f"`{sub}` service in {resource['name']}, save and restart.")
    return (f"In Coolify open {resource['name']} → Resource Limits, set Maximum Memory Limit to `{value}`, "
            "then restart it.")


def server_advice(rt: Runtime, server: dict[str, Any], resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current = loads(server.get("current_metrics"), {}) or {}
    if not current:
        return []
    now = time.time()
    uuid = server["uuid"]
    out: list[dict[str, Any]] = []

    def add(kind: str, severity: str, category: str, title: str, detail: str, action: str, *,
            resource: dict[str, Any] | None = None, container: str | None = None, saving: float | None = None,
            limit: int | None = None, **data: Any) -> None:
        out.append({"id": f"{kind}:{uuid}:{container or ''}", "kind": kind, "severity": severity,
                    "category": category, "title": title, "detail": detail, "action": action,
                    "server_uuid": uuid, "server_name": server["name"], "resource": resource,
                    "container": container, "saving": saving,
                    "limit": docker_size(limit) if limit else None,
                    "can_apply": bool(limit and resource and resource["kind"] in LIMIT_KINDS), **data})

    usage = _container_usage(rt, uuid, now - WINDOW)
    host = _host_memory(rt, uuid)
    mem_total = current.get("mem_total") or 0
    mem_free = max(0, mem_total - (current.get("mem_used") or 0))
    host_tight = max(host["median_day"] or 0, current.get("mem_pct") or 0) >= 85
    ncpu = current.get("ncpu") or 1
    running = [c for c in current.get("containers", []) if c.get("state") == "running"]

    for c in running:
        name = c.get("name") or ""
        ident = identity(name)
        resource = next((r for r in resources if r["uuid"] and r["uuid"] in name), None)
        if resource is None:
            continue  # not a Coolify resource (Coolify itself, the proxy); nothing to change in Coolify
        label = resource["name"] if resource["kind"] != "service" \
            else f"{resource['name']} ({ident.split('-' + resource['uuid'])[0]})"
        u = usage.get(ident) or {}
        mem, limit = c.get("mem_bytes"), c.get("mem_limit")
        peak = max(filter(None, (u.get("mem_max"), mem)), default=None)
        days = (u.get("coverage") or 0) / DAY
        common = {"resource": resource, "container": name, "mem": mem, "mem_peak": peak, "mem_limit": limit,
                  "cpu_avg": u.get("cpu_avg"), "days": days}

        # Close to its memory limit: the next spike gets it killed.
        if limit and peak and (peak >= 0.9 * limit or (mem or 0) >= 0.85 * limit or c.get("oom_killed")):
            new = suggest_limit(peak, 1.3)
            critical = c.get("oom_killed") or (mem or 0) >= 0.95 * limit
            room = "" if new - limit <= mem_free else (f" The server only has {human(mem_free)} free, so free memory "
                                                       "elsewhere first or move it to a bigger server.")
            add("mem_near_limit", "critical" if critical else "warning", "memory",
                f"{label} is close to its memory limit",
                f"Uses {human(mem)} now and peaked at {human(peak)} against a {human(limit)} limit"
                + (", and it has been killed for running out of memory." if c.get("oom_killed") else
                   ". At the limit the kernel kills it.") + room,
                _where_to_set(resource, name, docker_size(new)), limit=new, **common)
        # No limit: one runaway app can take the whole server down with it.
        elif not limit and peak and days >= 1:
            new = suggest_limit(peak, 1.5)
            add("mem_no_limit", "warning" if host_tight else "info", "memory", f"{label} has no memory limit",
                f"It peaked at {human(peak)} over the last {days:.0f} day(s). Without a limit, a leak or traffic spike in "
                "this app can use all the server's memory and take the other apps down with it.",
                _where_to_set(resource, name, docker_size(new)), limit=new, **common)
        # A limit far above real use protects nothing.
        elif limit and peak and days >= 3 and limit >= 4 * peak and limit - peak >= 512 * MB:
            new = suggest_limit(peak, 1.5)
            add("mem_limit_loose", "info", "memory", f"{label}'s memory limit is much higher than it needs",
                f"It never used more than {human(peak)} in {days:.0f} days but may use up to {human(limit)}. A limit "
                "doesn't reserve memory, so this frees nothing today; a tighter one stops a leak here from starving "
                "your other apps.", _where_to_set(resource, name, docker_size(new)), limit=new, **common)

        # A large share of the server's memory, while the server is short of it.
        if host_tight and mem and mem_total and mem >= 0.35 * mem_total:
            add("mem_heavy", "warning", "memory", f"{label} uses {100 * mem / mem_total:.0f}% of the server's memory",
                f"{human(mem)} of {human(mem_total)}, and the server is above 85% memory.",
                "Check its settings for worker or thread counts, cache and buffer sizes (for databases, e.g. "
                "shared_buffers / innodb_buffer_pool_size), give it a memory limit, or move it to a server with "
                "more memory.", **common)

        # Memory that only goes up: a leak, heading for an out-of-memory kill.
        started = _started(c.get("started_at"))
        if mem and started and now - started >= 7 * HOUR:
            series = _hourly_memory(rt, uuid, name, max(started + HOUR, now - WINDOW))
            fit = _trend(series)
            if fit and series[-1][0] - series[0][0] >= 6 * HOUR:
                slope, r2 = fit
                growth = slope * (series[-1][0] - series[0][0]) / HOUR
                if slope > 0 and r2 >= 0.8 and growth >= max(50 * MB, 0.2 * series[0][1]):
                    ceiling = limit or (mem + mem_free)
                    hours_left = max(0.0, (ceiling - mem) / slope)
                    what = "its limit" if limit else "the server's free memory"
                    add("mem_leak", "critical" if hours_left <= 6 else "warning" if hours_left <= 72 else "info",
                        "memory", f"{label}'s memory keeps growing",
                        f"Up {human(growth)} since it started, about {human(slope)} an hour, and it hasn't come back "
                        f"down. At this rate it reaches {what} in about {_duration(hours_left)}.",
                        "This usually means a leak (caches without a size limit, connections or objects never freed). "
                        "Restarting it resets memory for now; a memory limit makes the eventual restart automatic "
                        "instead of taking the server down.", hours_left=hours_left, growth_per_hour=slope, **common)

        # Busy enough to slow down its neighbours.
        cpu_avg = u.get("cpu_avg")
        if cpu_avg is not None and days >= 0.25 and cpu_avg >= max(80.0, 50.0 * ncpu):
            add("cpu_heavy", "warning", "cpu", f"{label} keeps the CPU busy",
                f"Averages {cpu_avg:.0f}% of one core (the server has {ncpu}). Other apps on this server wait for it.",
                "If that's expected load, give it a CPU limit in Coolify (Resource Limits → Number of CPUs) so the "
                "rest stay responsive, or move it to its own server. If it isn't, look for busy loops or runaway "
                "background jobs in its logs.", **common)
        # Idle for days: maybe not needed any more.
        elif (resource["kind"] != "database" and days >= 3 and cpu_avg is not None and cpu_avg < 0.2
              and (u.get("cpu_max") or 0) < 1.0 and (u.get("mem_avg") or 0) >= 100 * MB):
            add("idle", "info", "memory", f"{label} looks unused",
                f"Almost no CPU in {days:.0f} days (never above {u.get('cpu_max') or 0:.1f}% of a core) while holding "
                f"{human(u.get('mem_avg'))} of memory.",
                f"If nobody uses it any more, stop it in Coolify to free about {human(u.get('mem_avg'))}.",
                saving=u.get("mem_avg"), **common)

    # The server as a whole.
    if (host["median_day"] or 0) >= 85 or (current.get("swap_used") or 0) >= 512 * MB:
        top = sorted((c for c in running if c.get("mem_bytes")), key=lambda c: -c["mem_bytes"])[:3]
        names = ", ".join(f"{_container_label(c, resources)} {human(c['mem_bytes'])}" for c in top)
        add("server_mem_tight", "warning", "server", f"{server['name']} is short of memory",
            f"Memory sat around {host['median_day'] or current.get('mem_pct') or 0:.0f}% over the last day"
            + (f", peaking at {host['peak_day']:.0f}%" if host["peak_day"] else "")
            + (f" and {human(current.get('swap_used'))} is swapped out" if current.get("swap_used") else "")
            + f". Biggest users: {names}.",
            "Limit or tune the biggest users above, stop apps you don't need, or move to a server with more memory.")
    limits = sum(c.get("mem_limit") or 0 for c in running)
    if mem_total and limits > 1.2 * mem_total:
        add("server_overcommitted", "info", "server", f"Memory limits on {server['name']} add up to more than it has",
            f"Running containers may use up to {human(limits)} together on a server with {human(mem_total)}.",
            "That's fine while they stay well below their limits. If several peak together the server runs out, "
            "so keep the biggest limits close to what each app really needs.")
    if (host["week_span"] >= 6 * DAY and host["week_mem_max"] is not None and host["week_mem_max"] < 35
            and (host["week_cpu_max"] or 0) < 40 and mem_total >= 2 * GB):
        add("server_oversized", "info", "server", f"{server['name']} has plenty of room to spare",
            f"In the last week memory never went above {host['week_mem_max']:.0f}% and CPU averaged "
            f"{host['week_cpu_avg'] or 0:.0f}%.",
            "You could run more apps here, or move to a smaller, cheaper server.")

    # Disk, from the last disk report.
    report = disk.report_for(rt, server) if server.get("disk_at") and now - server["disk_at"] < WINDOW else None
    if report:
        risks = {f"disk_{i['key']}": i["risk"] for i in report["items"]}
        for f in disk.findings(report):
            # Freeable space is an opportunity, not an outage: at most a warning here.
            add(f["rule_id"], "warning" if f["severity"] == "critical" else f["severity"], "disk", f["title"],
                f["explanation"], " ".join(f["suggestions"]), saving=f["details"].get("bytes"),
                command=f["command"], risk=risks.get(f["rule_id"]))
    elif (current.get("disk_used_pct") or 0) >= 70:
        add("disk_unanalyzed", "info", "disk", f"Disk on {server['name']} is {current['disk_used_pct']:.0f}% full",
            "OpsMender can break down what uses the space (images, build cache, logs, backups) and what's safe "
            "to remove.", "Open the server and click Analyze disk usage. Nothing is deleted.")
    return out


def _container_label(c: dict[str, Any], resources: list[dict[str, Any]]) -> str:
    resource = next((r for r in resources if r["uuid"] and r["uuid"] in (c.get("name") or "")), None)
    return resource["name"] if resource else (c.get("name") or "?")


def _duration(hours: float) -> str:
    if hours < 1:
        return "under an hour"
    if hours < 48:
        return f"{hours:.0f} hours"
    return f"{hours / 24:.0f} days"


def all_advice(rt: Runtime, server_uuid: str | None = None) -> list[dict[str, Any]]:
    resources = rt.db.all("SELECT uuid, kind, name, server_uuid FROM resources WHERE missing = 0")
    servers = rt.db.all("SELECT * FROM servers WHERE metrics_enabled = 1" + (" AND uuid = ?" if server_uuid else "")
                        + " ORDER BY name", (server_uuid,) if server_uuid else ())
    items = [a for s in servers for a in server_advice(rt, s, resources)]
    items.sort(key=lambda a: (SEVERITY_ORDER[a["severity"]], a["category"], a["server_name"], a["title"]))
    return items


def summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "critical": sum(1 for a in items if a["severity"] == "critical"),
        "warning": sum(1 for a in items if a["severity"] == "warning"),
        "info": sum(1 for a in items if a["severity"] == "info"),
        "no_limit": sum(1 for a in items if a["kind"] == "mem_no_limit"),
        "idle_memory": sum(a["saving"] or 0 for a in items if a["kind"] == "idle"),
        "disk_freeable": sum(a["saving"] or 0 for a in items if a.get("risk") in {"safe", "low"}),
    }


def check_leaks(rt: Runtime) -> None:
    """Open an incident when a growing app is predicted to run out of memory within a day."""
    leaking: set[str] = set()
    for a in all_advice(rt):
        if a["kind"] != "mem_leak" or a["hours_left"] > LEAK_ALERT_HOURS:
            continue
        fp = f"leak:{a['server_uuid']}:{identity(a['container'])}"
        leaking.add(fp)
        open_incident(rt, fingerprint=fp, kind="memory_leak",
                      severity="critical" if a["hours_left"] <= 6 else "warning",
                      title=a["title"], summary=a["detail"], resource=a["resource"], server_uuid=a["server_uuid"],
                      evidence={"advice": {k: a.get(k) for k in ("mem", "mem_limit", "growth_per_hour", "hours_left")}})
    for fp in open_fingerprints(rt, "leak:") - leaking:
        resolve_incident(rt, fp, "memory stopped growing")


def cached_summary(rt: Runtime, max_age: float = 60.0) -> dict[str, Any]:
    """summary() of all advice, reused for a minute: metrics only change once a minute anyway."""
    at, value = rt.status.get("advice_summary", (0.0, None))
    if value is None or time.time() - at > max_age:
        value = summary(all_advice(rt))
        rt.status["advice_summary"] = (time.time(), value)
    return value
