"""Right-sizing advice across servers, and applying a suggested memory limit through Coolify."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request

from ...adapters.coolify import CoolifyError
from ...advice import CATEGORY_LABELS, LIMIT_KINDS, all_advice, summary, valid_limit
from ...incidents import resource_row
from ..common import current_user, flash, form_with_csrf, redirect, render, rt_of

router = APIRouter()


@router.get("/optimize")
async def optimize(request: Request, user: str = Depends(current_user)):
    rt = rt_of(request)
    server = request.query_params.get("server") or None
    items = all_advice(rt, server)
    servers = rt.db.all("SELECT uuid, name FROM servers WHERE metrics_enabled = 1 ORDER BY name")
    return render(request, "optimize.html", items=items, summary=summary(items), categories=CATEGORY_LABELS,
                  servers=servers, server=server, actions_enabled=rt.settings.bool("actions_enabled"),
                  any_metrics=bool(servers))


def _limit_target(request: Request, uuid: str, limit: str) -> dict:
    resource = resource_row(rt_of(request), uuid)
    if not resource:
        raise HTTPException(404, "Resource not found")
    if resource["kind"] not in LIMIT_KINDS or not valid_limit(limit):
        raise HTTPException(400, "Coolify can only set a memory limit like 512m or 2g on applications and databases")
    return resource


@router.get("/optimize/apply")
async def confirm_limit(request: Request, user: str = Depends(current_user)):
    rt = rt_of(request)
    resource = _limit_target(request, request.query_params.get("resource", ""), request.query_params.get("limit", ""))
    current = (resource.get("raw") or {}).get("limits_memory")
    return render(request, "optimize_apply.html", r=resource, limit=request.query_params["limit"],
                  current=None if current in (None, "", "0") else current,
                  actions_enabled=rt.settings.bool("actions_enabled"))


@router.post("/optimize/apply")
async def apply_limit(request: Request, user: str = Depends(current_user)):
    form = await form_with_csrf(request)
    rt = rt_of(request)
    resource = _limit_target(request, form.get("resource", ""), form.get("limit", ""))
    limit, uuid = form["limit"], resource["uuid"]
    back = f"/optimize/apply?resource={uuid}&limit={limit}"
    if not rt.settings.bool("actions_enabled"):
        flash(request, "Actions are turned off. Enable them in Settings → Actions.", "error")
        return redirect(back)
    if form.get("confirm") != resource["name"]:
        flash(request, f"Type the resource name ({resource['name']}) to confirm.", "error")
        return redirect(back)
    client = rt.coolify()
    if client is None:
        flash(request, "Coolify isn't connected.", "error")
        return redirect(back)
    try:
        message = await client.set_memory_limit(resource["kind"], uuid, limit)
    except CoolifyError as exc:
        rt.db.audit(user, "set_memory_limit", "error", resource["kind"], uuid, resource["name"], f"{limit}: {exc}")
        flash(request, f"Couldn't set the limit: {exc}", "error")
        return redirect(back)
    rt.db.audit(user, "set_memory_limit", "ok", resource["kind"], uuid, resource["name"], limit)
    rt.db.add_event("action", f"{user} set the memory limit of {resource['name']} to {limit}", "info", uuid)
    flash(request, f"{message} Restart {resource['name']} for the new limit to take effect.", "success")
    asyncio.get_running_loop().call_later(3, rt.wake.set)
    return redirect(f"/resources/{uuid}")
