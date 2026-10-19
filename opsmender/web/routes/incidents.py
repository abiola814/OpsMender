"""Incident list and detail, AI diagnosis, re-collecting evidence, manual resolve."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request

from ...ai.providers import AIError
from ...db import dumps, loads
from ...incidents import ai_config, diagnose, enrich, resource_row
from ...metrics.disk import RISK_TEXT
from ..common import current_user, flash, form_with_csrf, redirect, render, rt_of
from .resources import ACTION_LABELS, actions_for

router = APIRouter()


@router.get("/incidents")
async def incidents(request: Request, user: str = Depends(current_user)):
    rt = rt_of(request)
    status = request.query_params.get("status", "open")
    where = "" if status == "all" else "WHERE status = ?"
    params = () if status == "all" else (status,)
    rows = rt.db.all(f"SELECT * FROM incidents {where} ORDER BY status = 'open' DESC, last_seen DESC LIMIT 300", params)
    return render(request, "incidents.html", rows=rows, current=status)


@router.get("/incidents/{incident_id}")
async def incident_detail(request: Request, incident_id: int, user: str = Depends(current_user)):
    rt = rt_of(request)
    inc = rt.db.one("SELECT * FROM incidents WHERE id = ?", (incident_id,))
    if not inc:
        raise HTTPException(404, "Incident not found")
    resource = resource_row(rt, inc["resource_uuid"])
    server = rt.db.one("SELECT uuid, name FROM servers WHERE uuid = ?", (inc["server_uuid"],)) if inc["server_uuid"] else None
    cfg = ai_config(rt)
    return render(
        request, "incident.html", inc=inc, resource=resource, server=server,
        evidence=loads(inc["evidence"], {}) or {}, findings=loads(inc["findings"], {}) or {},
        ai_ready=bool(cfg), ai_label=f"{cfg.preset.label} · {cfg.model}" if cfg else None,
        ai_running=incident_id in rt.ai_inflight,
        actions_enabled=rt.settings.bool("actions_enabled"),
        actions=actions_for(resource) if resource else [], action_labels=ACTION_LABELS,
        events=rt.db.all("SELECT * FROM events WHERE incident_id = ? ORDER BY ts", (incident_id,)),
        risk_text=RISK_TEXT,
    )


@router.post("/incidents/{incident_id}/diagnose")
async def run_diagnosis(request: Request, incident_id: int, user: str = Depends(current_user)):
    await form_with_csrf(request)
    rt = rt_of(request)
    try:
        await diagnose(rt, incident_id)
        rt.db.audit(user, "ai_diagnose", "ok", "incident", str(incident_id))
        flash(request, "AI diagnosis complete.", "success")
    except AIError as exc:
        rt.db.audit(user, "ai_diagnose", "error", "incident", str(incident_id), detail=str(exc))
        flash(request, f"AI diagnosis failed: {exc}", "error")
    return redirect(f"/incidents/{incident_id}#ai")


@router.post("/incidents/{incident_id}/refresh")
async def refresh_evidence(request: Request, incident_id: int, user: str = Depends(current_user)):
    await form_with_csrf(request)
    rt = rt_of(request)
    inc = rt.db.one("SELECT evidence FROM incidents WHERE id = ?", (incident_id,))
    if not inc:
        raise HTTPException(404, "Incident not found")
    evidence = loads(inc["evidence"], {}) or {}
    # Keep deployment build logs (they don't change); re-fetch runtime logs, metrics and container state.
    keep = {k: v for k, v in evidence.items() if k in {"deployment_uuid", "forecast", "disks", "top_containers"} or
            (k in {"logs", "log_source"} and evidence.get("log_source") == "Coolify deployment log")}
    rt.db.execute("UPDATE incidents SET evidence = ? WHERE id = ?", (dumps(keep), incident_id))
    await enrich(rt, incident_id)
    flash(request, "Evidence refreshed.", "success")
    return redirect(f"/incidents/{incident_id}")


@router.post("/incidents/{incident_id}/resolve")
async def resolve(request: Request, incident_id: int, user: str = Depends(current_user)):
    await form_with_csrf(request)
    rt = rt_of(request)
    inc = rt.db.one("SELECT * FROM incidents WHERE id = ? AND status = 'open'", (incident_id,))
    if inc:
        rt.db.execute("UPDATE incidents SET status = 'resolved', resolved_at = ? WHERE id = ?", (time.time(), incident_id))
        rt.db.add_event("incident_resolved", f"{user} resolved: {inc['title']}", "info", inc["resource_uuid"], incident_id)
        rt.db.audit(user, "resolve_incident", "ok", "incident", str(incident_id), inc["title"])
    return redirect(f"/incidents/{incident_id}")
