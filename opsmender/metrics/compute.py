"""Turn two consecutive raw probe snapshots into usage figures.

The probe reports cumulative counters (CPU jiffies, cgroup usage_usec); rates come from the
difference between this snapshot and the previous one.
"""

from __future__ import annotations

from typing import Any


def root_disk(disks: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not disks:
        return None
    for d in disks:
        if d.get("mount") == "/":
            return d
    return max(disks, key=lambda d: d.get("total_kb") or 0)


def compute(prev: dict[str, Any] | None, cur: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"ts": cur.get("ts"), "hostname": cur.get("hostname"), "ncpu": cur.get("ncpu"),
                           "uptime_s": cur.get("uptime_s"), "docker": cur.get("docker", False),
                           "probe_v": cur.get("v", 1)}

    cpu_pct = None
    if prev and prev.get("cpu") and cur.get("cpu"):
        d_total = cur["cpu"]["total"] - prev["cpu"]["total"]
        d_idle = cur["cpu"]["idle"] - prev["cpu"]["idle"]
        if d_total > 0:
            cpu_pct = max(0.0, min(100.0, 100.0 * (1 - d_idle / d_total)))
    out["cpu_pct"] = cpu_pct

    load = cur.get("load") or [None, None, None]
    out["load1"] = load[0] if load else None
    out["load"] = load

    mem = cur.get("mem") or {}
    total = (mem.get("total_kb") or 0) * 1024
    available = (mem.get("available_kb") or 0) * 1024
    out["mem_total"] = total
    out["mem_used"] = max(0, total - available) if total else None
    out["mem_pct"] = 100.0 * out["mem_used"] / total if total else None
    swap_total = (mem.get("swap_total_kb") or 0) * 1024
    out["swap_used"] = max(0, swap_total - (mem.get("swap_free_kb") or 0) * 1024) if swap_total else 0

    disks = []
    for d in cur.get("disks") or []:
        total_kb = d.get("total_kb") or 0
        used_kb = d.get("used_kb") or 0
        avail_kb = d.get("avail_kb") or 0
        # Same formula as df: used / (used + available), which accounts for reserved blocks.
        denom = used_kb + avail_kb
        disks.append({"mount": d.get("mount"), "total": total_kb * 1024, "used": used_kb * 1024,
                      "pct": 100.0 * used_kb / denom if denom else None})
    out["disks"] = disks
    rd = root_disk(disks)
    out["disk_used_pct"] = rd["pct"] if rd else None
    out["disk_used"] = rd["used"] if rd else None
    out["disk_total"] = rd["total"] if rd else None

    prev_containers = {c.get("id"): c for c in (prev or {}).get("containers") or []}
    elapsed = (cur.get("ts") or 0) - ((prev or {}).get("ts") or 0)
    containers = []
    for c in cur.get("containers") or []:
        item = {
            "id": c.get("id"), "name": c.get("name"), "state": c.get("state"), "health": c.get("health") or None,
            "restarts": c.get("restarts") or 0, "oom_killed": bool(c.get("oom_killed")),
            "exit_code": c.get("exit_code"), "started_at": c.get("started_at"),
            "mem_bytes": c.get("mem_bytes"), "mem_limit": c.get("mem_limit"), "cpu_pct": None,
        }
        before = prev_containers.get(c.get("id"))
        if before and elapsed > 0 and c.get("cpu_usec") is not None and before.get("cpu_usec") is not None:
            delta = c["cpu_usec"] - before["cpu_usec"]
            if delta >= 0:
                # Percent of one core, like `docker stats` (can exceed 100 on multi-core hosts).
                item["cpu_pct"] = 100.0 * delta / (elapsed * 1_000_000)
        containers.append(item)
    containers.sort(key=lambda c: (c["state"] != "running", -(c["mem_bytes"] or 0)))
    out["containers"] = containers
    return out
