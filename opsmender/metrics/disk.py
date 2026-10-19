"""Explain what is using disk space on a server, and what each cleanup option would cost.

Read-only: this module never deletes anything. The probe's ``disk`` request supplies the raw numbers.
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..db import dumps, loads
from ..runtime import Runtime
from . import ssh

PROBE_VERSION = 2
JOURNAL_KEEP = 500 * 1_000_000
_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([kKMGTP]?i?B)?")
_UNITS = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "PB": 1e15,
          "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}

# Docker `system df` types, in the order people usually reclaim them.
CATEGORIES: dict[str, dict[str, Any]] = {
    "Build Cache": {
        "key": "build_cache", "label": "Build cache", "risk": "safe",
        "explanation": "Layers left over from building images. Removing them only makes the next build slower.",
        "command": "docker builder prune",
    },
    "Images": {
        "key": "images", "label": "Unused images", "risk": "medium",
        "explanation": "Images no running container uses. Untagged (dangling) images are always safe to remove. "
                       "Tagged ones include the previous versions Coolify keeps so you can roll back, so removing "
                       "them removes those rollback points.",
        "command": "docker image prune      # untagged only (safe)\n"
                   "docker image prune -a   # every unused image (removes rollback points)",
    },
    "Containers": {
        "key": "containers", "label": "Stopped containers", "risk": "low",
        "explanation": "Stopped containers and their writable layers. Check none of them is something you stopped "
                       "on purpose and plan to start again.",
        "command": "docker container prune",
    },
    "Local Volumes": {
        "key": "volumes", "label": "Unused volumes", "risk": "danger",
        "explanation": "Volumes hold persistent data such as databases and uploads. \"Unused\" only means no "
                       "container is attached right now, not that the data is unneeded. Review them one by one "
                       "and never delete them in bulk.",
        "command": "docker volume ls -f dangling=true   # list only; inspect each one before removing it",
    },
}
RISK_ORDER = {"safe": 0, "low": 1, "medium": 2, "danger": 3}


def parse_size(value: Any) -> int | None:
    """Parse Docker's human sizes ("45.84GB", "93.1MB", "0B", "2.5 GiB") into bytes."""
    if value in (None, ""):
        return None
    match = _SIZE_RE.match(str(value))
    if not match:
        return None
    unit = (match.group(2) or "B").upper()
    return int(float(match.group(1)) * _UNITS.get(unit, 1))


def build_report(raw: dict[str, Any], containers: list[dict[str, Any]] | None = None,
                 resources: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Turn the probe's raw output into categories with sizes, risk and plain explanations."""
    names = {c.get("id"): c.get("name") for c in containers or [] if c.get("id")}
    items: list[dict[str, Any]] = []

    for entry in raw.get("system_df") or []:
        info = CATEGORIES.get(entry.get("Type"))
        if not info:
            continue
        items.append({**info, "size": parse_size(entry.get("Size")),
                      "reclaimable": parse_size(entry.get("Reclaimable")),
                      "count": _int(entry.get("TotalCount")), "active": _int(entry.get("Active"))})

    logs = raw.get("container_logs") or {}
    driver = raw.get("log_driver") or ""
    rotation = bool(raw.get("log_rotation")) or driver in {"local", "journald", "none", "syslog"}
    if logs.get("total_bytes"):
        items.append({
            "key": "container_logs", "label": "Container logs", "risk": "low",
            "size": logs["total_bytes"], "reclaimable": logs["total_bytes"], "count": logs.get("count"),
            "explanation": "Output your containers have printed. With Docker's default json-file driver these files "
                           "grow forever unless log rotation is on. Emptying one keeps the container running but "
                           "loses its old log lines.",
            "command": "truncate -s 0 \"$(docker inspect --format '{{.LogPath}}' <container>)\"",
        })
    journal = (raw.get("journal_kb") or 0) * 1024
    if journal:
        items.append({
            "key": "journal", "label": "System journal", "risk": "safe",
            "size": journal, "reclaimable": max(0, journal - JOURNAL_KEEP),
            "explanation": "systemd's own logs. Shrinking the journal keeps the most recent entries.",
            "command": "journalctl --vacuum-size=500M",
        })
    backups = (raw.get("coolify_backups_kb") or 0) * 1024
    if backups:
        items.append({
            "key": "coolify_backups", "label": "Coolify backups", "risk": "medium",
            "size": backups, "reclaimable": None,
            "explanation": "Database backups Coolify stores on this server (/data/coolify/backups). Keep the ones "
                           "you need. Setting a retention limit or S3 backups in Coolify stops them piling up.",
            "command": "ls -lh /data/coolify/backups",
        })
    items.sort(key=lambda i: (-(i["reclaimable"] or 0), -(i["size"] or 0)))

    top_logs = []
    for entry in logs.get("top") or []:
        name = names.get(entry.get("id")) or entry.get("id")
        resource = next((r for r in resources or [] if r.get("uuid") and r["uuid"] in (name or "")), None)
        top_logs.append({"id": entry.get("id"), "name": name, "bytes": entry.get("bytes"), "resource": resource})

    fs = raw.get("fs") or {}
    fs_total = (fs.get("total_kb") or 0) * 1024
    fs_used = (fs.get("used_kb") or 0) * 1024
    fs_avail = (fs.get("avail_kb") or 0) * 1024
    low_risk = sum(i["reclaimable"] or 0 for i in items if i["risk"] in {"safe", "low"})
    return {
        "ts": raw.get("ts"), "docker": raw.get("docker", False), "docker_root": raw.get("docker_root"),
        "log_driver": driver, "log_rotation": rotation,
        "fs": {"mount": fs.get("mount"), "total": fs_total, "used": fs_used, "avail": fs_avail,
               "pct": 100.0 * fs_used / (fs_used + fs_avail) if fs_used + fs_avail else None},
        "items": items, "top_logs": top_logs, "low_risk_reclaimable": low_risk,
        "var_log": (raw.get("var_log_kb") or 0) * 1024,
    }


def findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Rule-style findings for a disk incident, biggest win first."""
    out: list[dict[str, Any]] = []
    total = report["fs"]["total"] or 0
    for item in report["items"]:
        reclaim = item["reclaimable"] or 0
        if reclaim < 1_000_000_000 and not (total and reclaim >= 0.05 * total):
            continue
        severity = "critical" if total and reclaim >= 0.15 * total else "warning"
        verb = "can be freed" if item["risk"] in {"safe", "low"} else "is unused, but needs review"
        out.append({
            "rule_id": f"disk_{item['key']}", "title": f"{item['label']}: {human(reclaim)} {verb}",
            "category": "disk", "severity": severity, "explanation": item["explanation"],
            "suggestions": [f"Risk: {item['risk']}. " + RISK_TEXT[item["risk"]]], "matches": 0,
            "sample": "", "command": item["command"], "details": {"bytes": reclaim},
        })
    logs = next((i for i in report["items"] if i["key"] == "container_logs"), None)
    if not report["log_rotation"] and report["docker"]:
        size = human(logs["size"]) if logs else "0 B"
        out.append({
            "rule_id": "disk_log_rotation", "title": "Docker log rotation is off", "category": "disk",
            "severity": "warning" if (logs and logs["size"] > 1_000_000_000) else "info",
            "explanation": f"Container logs use {size} and will keep growing: the {report['log_driver'] or 'json-file'} "
                           "driver is not limited in /etc/docker/daemon.json.",
            "suggestions": ["Add log limits to /etc/docker/daemon.json, then restart Docker during a quiet period "
                            "(running containers restart with it). Containers created afterwards use the limits."],
            "matches": 0, "sample": "",
            "command": '{\n  "log-driver": "json-file",\n  "log-opts": {"max-size": "10m", "max-file": "3"}\n}',
            "details": {},
        })
    return out


RISK_TEXT = {
    "safe": "Nothing you need is lost.",
    "low": "Usually fine; check the list first.",
    "medium": "Can remove things you may want back (rollback images, backups).",
    "danger": "Can permanently delete data. Review item by item.",
}


def human(n: float | None) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


async def collect(rt: Runtime, server: dict[str, Any]) -> dict[str, Any]:
    """Run the probe's disk request, store the raw result, and return the built report."""
    key = rt.settings.get("ssh_private_key")
    if not key or not server.get("metrics_enabled"):
        raise ssh.ProbeError("Enable metrics on this server first.")
    try:
        raw = await ssh.disk(server.get("probe_host") or server["ip"], server.get("probe_port") or 22, key,
                             server.get("host_key"))
    except ssh.ProbeError as exc:
        if "unsupported request" in str(exc):
            raise ssh.ProbeError("This server runs an older OpsMender probe. Download the setup script again "
                                 "and re-run it to update.") from exc
        raise
    rt.db.execute("UPDATE servers SET disk_raw = ?, disk_at = ? WHERE uuid = ?", (dumps(raw), time.time(), server["uuid"]))
    return report_for(rt, {**server, "disk_raw": dumps(raw)})


def report_for(rt: Runtime, server: dict[str, Any]) -> dict[str, Any] | None:
    raw = loads(server.get("disk_raw"))
    if not raw:
        return None
    current = loads(server.get("current_metrics"), {}) or {}
    resources = rt.db.all("SELECT uuid, name, kind FROM resources WHERE missing = 0")
    return build_report(raw, current.get("containers"), resources)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
