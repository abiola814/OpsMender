"""Overview, activity feed, audit log, manual sync."""

from __future__ import annotations

import time
from collections import Counter
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request

from ...adapters.base import health_of
from ...db import loads
from ...metrics.store import history
from ..common import current_user, flash, form_with_csrf, redirect, render, rt_of

router = APIRouter()


def server_cards(rt) -> list[dict]:
    cards = []
    for s in rt.db.all("SELECT * FROM servers ORDER BY name"):
        current = loads(s.get("current_metrics"), {}) or {}
        s["m"] = current
        s["series"] = history(rt.db, s["uuid"], hours=1) if s["metrics_enabled"] else []
        s["running"] = sum(1 for c in current.get("containers", []) if c.get("state") == "running")
        cards.append(s)
    return cards


@router.get("/")
async def dashboard(request: Request, user: str = Depends(current_user)):
    rt = rt_of(request)
    resources = rt.db.all("SELECT * FROM resources WHERE missing = 0 ORDER BY name")
    health_counts = Counter(health_of(r["status"]) for r in resources)
    with_incidents = {r["resource_uuid"] for r in rt.db.all(
        "SELECT DISTINCT resource_uuid FROM incidents WHERE status = 'open' AND resource_uuid IS NOT NULL")}
    # Stopped resources only need attention if they stopped unexpectedly (an open incident says so).
    attention = [r for r in resources if health_of(r["status"]) in {"down", "warning"}
                 or (health_of(r["status"]) == "stopped" and r["uuid"] in with_incidents)]
    since = time.time() - 86400
    deploys = rt.db.all("SELECT status FROM deployments WHERE seen_at >= ? AND final = 1", (since,))
    return render(
        request, "dashboard.html",
        servers=server_cards(rt),
        resources=resources,
        health_counts=health_counts,
        attention=attention,
        incidents=rt.db.all("SELECT * FROM incidents WHERE status = 'open' ORDER BY severity = 'critical' DESC,"
                            " last_seen DESC LIMIT 8"),
        events=rt.db.all("SELECT * FROM events ORDER BY ts DESC LIMIT 25"),
        deploy_ok=sum(1 for d in deploys if d["status"].lower() in {"finished", "success"}),
        deploy_failed=sum(1 for d in deploys if d["status"].lower() in {"failed", "error"}),
        running_deploys=rt.db.all("SELECT * FROM deployments WHERE final = 0 ORDER BY seen_at DESC"),
        thresholds={k: rt.settings.int(k) for k in ("cpu_threshold", "mem_threshold", "disk_threshold")},
    )


@router.post("/sync")
async def sync_now(request: Request, user: str = Depends(current_user)):
    await form_with_csrf(request)
    rt_of(request).wake.set()
    flash(request, "Sync requested. Fresh data will appear in a few seconds.", "info")
    back = urlsplit(request.headers.get("referer") or "/").path or "/"
    return redirect(back if back.startswith("/") and not back.startswith("//") else "/")


@router.get("/activity")
async def activity(request: Request, user: str = Depends(current_user)):
    rt = rt_of(request)
    return render(request, "activity.html",
                  events=rt.db.all("SELECT * FROM events ORDER BY ts DESC LIMIT 300"),
                  audit=rt.db.all("SELECT * FROM audit ORDER BY ts DESC LIMIT 200"))


@router.get("/healthz")
async def healthz():
    return {"ok": True}
