"""Applications, services and databases: list, detail, logs, and confirmed actions."""

from __future__ import annotations

import asyncio
import time
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ...adapters.base import health_of
from ...adapters.coolify import CoolifyError
from ...diagnosis.rules import analyze
from ...incidents import container_for, fetch_logs, resource_row
from ...metrics.store import container_history
from ..common import current_user, flash, form_with_csrf, redirect, render, rt_of

router = APIRouter()
ACTION_LABELS = {"restart": "Restart", "redeploy": "Redeploy", "stop": "Stop", "start": "Start"}


@router.get("/resources")
async def resources(request: Request, user: str = Depends(current_user)):
    rt = rt_of(request)
    kind = request.query_params.get("kind", "")
    state = request.query_params.get("state", "")
    rows = rt.db.all("SELECT r.*, s.name AS server_name FROM resources r LEFT JOIN servers s ON s.uuid = r.server_uuid"
                     " WHERE r.missing = 0 ORDER BY r.name")
    for r in rows:
        r["health"] = health_of(r["status"])
    counts = Counter(r["health"] for r in rows)
    kinds = Counter(r["kind"] for r in rows)
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    if state:
        rows = [r for r in rows if r["health"] == state]
    return render(request, "resources.html", rows=rows, counts=counts, kinds=kinds, kind=kind, state=state)


@router.get("/resources/{uuid}")
async def resource_detail(request: Request, uuid: str, user: str = Depends(current_user)):
    rt = rt_of(request)
    resource = resource_row(rt, uuid)
    if not resource:
        raise HTTPException(404, "Resource not found")
    server = rt.db.one("SELECT * FROM servers WHERE uuid = ?", (resource["server_uuid"],)) if resource["server_uuid"] else None
    metrics_server, container = container_for(rt, resource)
    series = container_history(rt.db, metrics_server["uuid"], container["name"]) if container else []
    for point in series:
        point["mem_mb"] = (point["mem_bytes"] or 0) / 1_048_576
    mem_cap = max([p["mem_mb"] for p in series] + [1]) * 1.15
    return render(
        request, "resource.html", r=resource, server=server, container=container, series=series, mem_cap=mem_cap,
        health=health_of(resource["status"]),
        deployments=rt.db.all("SELECT * FROM deployments WHERE app_uuid = ? ORDER BY created_at DESC LIMIT 15", (uuid,)),
        incidents=rt.db.all("SELECT * FROM incidents WHERE resource_uuid = ? ORDER BY status = 'open' DESC,"
                            " last_seen DESC LIMIT 10", (uuid,)),
        actions_enabled=rt.settings.bool("actions_enabled"),
        actions=actions_for(resource),
        action_labels=ACTION_LABELS,
    )


@router.get("/resources/{uuid}/logs")
async def resource_logs(request: Request, uuid: str, user: str = Depends(current_user)):
    rt = rt_of(request)
    resource = resource_row(rt, uuid)
    if not resource:
        raise HTTPException(404, "Resource not found")
    text, source = await fetch_logs(rt, resource)
    if not text.strip():
        hint = ("Coolify only exposes logs for applications. Enable metrics on this resource's server to read "
                "service and database logs through the probe.") if resource["kind"] != "application" else \
               "No log output returned."
        return JSONResponse({"logs": "", "source": source, "hint": hint, "analysis": None})
    return JSONResponse({"logs": text, "source": source, "analysis": analyze(text)})


@router.post("/resources/{uuid}/action")
async def resource_action(request: Request, uuid: str, user: str = Depends(current_user)):
    form = await form_with_csrf(request)
    rt = rt_of(request)
    resource = resource_row(rt, uuid)
    if not resource:
        raise HTTPException(404, "Resource not found")
    action = form.get("action", "")
    back = form.get("back") or f"/resources/{uuid}"
    if not back.startswith("/") or back.startswith("//"):
        back = f"/resources/{uuid}"
    if not rt.settings.bool("actions_enabled"):
        flash(request, "Actions are turned off. Enable them in Settings → Actions.", "error")
        return redirect(back)
    if action not in actions_for(resource):
        flash(request, f"'{action}' isn't available for this {resource['kind']}.", "error")
        return redirect(back)
    if form.get("confirm") != resource["name"]:
        flash(request, f"Type the resource name ({resource['name']}) to confirm.", "error")
        return redirect(back)
    client = rt.coolify()
    if client is None:
        flash(request, "Coolify isn't connected.", "error")
        return redirect(back)
    try:
        message = await client.perform(resource["kind"], uuid, action)
    except CoolifyError as exc:
        rt.db.audit(user, action, "error", resource["kind"], uuid, resource["name"], str(exc))
        rt.db.add_event("action_failed", f"{ACTION_LABELS[action]} {resource['name']} failed: {exc}", "error", uuid)
        flash(request, f"{ACTION_LABELS[action]} failed: {exc}", "error")
        return redirect(back)
    if action == "stop":
        rt.suppressed[uuid] = time.time() + 3600
    elif action in {"restart", "redeploy", "start"}:
        rt.suppressed[uuid] = time.time() + 300
    rt.db.audit(user, action, "ok", resource["kind"], uuid, resource["name"], message)
    rt.db.add_event("action", f"{user} requested {action} of {resource['name']}: {message}", "info", uuid)
    flash(request, f"{ACTION_LABELS[action]} requested: {message}", "success")
    asyncio.get_running_loop().call_later(8, rt.wake.set)
    return redirect(back)


def actions_for(resource: dict) -> list[str]:
    if resource["kind"] == "application":
        return ["restart", "redeploy", "stop", "start"]
    return ["restart", "stop", "start"]
