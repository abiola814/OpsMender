"""Server list, server detail with metrics, and the probe setup flow."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from ...db import loads
from ...metrics import disk, ssh
from ...metrics.installer import install_script
from ...metrics.store import disk_forecast, disk_series, history
from ...monitor.metrics import poll_server
from ..common import current_user, flash, form_with_csrf, redirect, render, rt_of
from .dashboard import server_cards

router = APIRouter()
LOCAL_HOSTS = {"host.docker.internal", "localhost", "127.0.0.1", "::1"}
RANGES = {"1h": 1, "24h": 24, "7d": 168, "30d": 720}


def _server(rt, uuid: str) -> dict:
    server = rt.db.one("SELECT * FROM servers WHERE uuid = ?", (uuid,))
    if not server:
        raise HTTPException(404, "Server not found")
    return server


def ensure_keypair(rt) -> str:
    private = rt.settings.get("ssh_private_key")
    if not private:
        private, _ = ssh.generate_keypair()
        rt.settings.set("ssh_private_key", private)
    return ssh.public_key_of(private)


@router.get("/servers")
async def servers(request: Request, user: str = Depends(current_user)):
    rt = rt_of(request)
    return render(request, "servers.html", servers=server_cards(rt),
                  thresholds={k: rt.settings.int(k) for k in ("cpu_threshold", "mem_threshold", "disk_threshold")})


@router.get("/servers/{uuid}")
async def server_detail(request: Request, uuid: str, user: str = Depends(current_user)):
    rt = rt_of(request)
    server = _server(rt, uuid)
    range_key = request.query_params.get("range", "24h")
    hours = RANGES.get(range_key, 24)
    current = loads(server.get("current_metrics"), {}) or {}
    resources = {r["uuid"]: r for r in rt.db.all("SELECT uuid, name, kind FROM resources")}
    for c in current.get("containers", []):
        c["resource"] = next((r for u, r in resources.items() if u and u in (c.get("name") or "")), None)
    public_key = ensure_keypair(rt) if server["metrics_enabled"] else None
    return render(
        request, "server.html", server=server, m=current, range_key=range_key, ranges=list(RANGES),
        series=history(rt.db, uuid, hours=hours) if server["metrics_enabled"] else [],
        forecast=disk_forecast(disk_series(rt.db, uuid)) if server["metrics_enabled"] else None,
        public_key=public_key,
        disk_report=disk.report_for(rt, server), risk_text=disk.RISK_TEXT, probe_current=disk.PROBE_VERSION,
        hosted=rt.db.all("SELECT * FROM resources WHERE server_uuid = ? AND missing = 0 ORDER BY name", (uuid,)),
        incidents=rt.db.all("SELECT * FROM incidents WHERE server_uuid = ? ORDER BY status = 'open' DESC,"
                            " last_seen DESC LIMIT 10", (uuid,)),
        thresholds={k: rt.settings.int(k) for k in ("cpu_threshold", "mem_threshold", "disk_threshold")},
    )


@router.post("/servers/{uuid}/metrics/enable")
async def enable_metrics(request: Request, uuid: str, user: str = Depends(current_user)):
    await form_with_csrf(request)
    rt = rt_of(request)
    server = _server(rt, uuid)
    ensure_keypair(rt)
    host = server["probe_host"] or ("host.docker.internal" if (server["ip"] or "") in LOCAL_HOSTS else server["ip"])
    rt.db.execute("UPDATE servers SET metrics_enabled = 1, probe_host = ?, probe_port = COALESCE(probe_port, ?)"
                  " WHERE uuid = ?", (host, server["port"] or 22, uuid))
    rt.db.audit(user, "enable_metrics", "ok", "server", uuid, server["name"])
    flash(request, "Metrics enabled. Run the setup script below on the server, then click 'Test connection'.", "success")
    return redirect(f"/servers/{uuid}#setup")


@router.post("/servers/{uuid}/metrics/disable")
async def disable_metrics(request: Request, uuid: str, user: str = Depends(current_user)):
    await form_with_csrf(request)
    rt = rt_of(request)
    server = _server(rt, uuid)
    rt.db.execute("UPDATE servers SET metrics_enabled = 0, last_snapshot = NULL, current_metrics = NULL WHERE uuid = ?",
                  (uuid,))
    rt.db.audit(user, "disable_metrics", "ok", "server", uuid, server["name"])
    flash(request, "Metrics disabled. To remove the probe from the server: "
                   "userdel -r opsmender; rm -rf /usr/local/lib/opsmender /etc/sudoers.d/opsmender", "info")
    return redirect(f"/servers/{uuid}")


@router.post("/servers/{uuid}/metrics/settings")
async def metrics_settings(request: Request, uuid: str, user: str = Depends(current_user)):
    form = await form_with_csrf(request)
    rt = rt_of(request)
    _server(rt, uuid)
    host = form.get("probe_host", "").strip() or None
    try:
        port = int(form.get("probe_port") or 22)
    except ValueError:
        port = 22
    rt.db.execute("UPDATE servers SET probe_host = ?, probe_port = ? WHERE uuid = ?", (host, port, uuid))
    flash(request, "Connection settings saved.", "success")
    return redirect(f"/servers/{uuid}#setup")


@router.post("/servers/{uuid}/metrics/reset-hostkey")
async def reset_hostkey(request: Request, uuid: str, user: str = Depends(current_user)):
    await form_with_csrf(request)
    rt = rt_of(request)
    server = _server(rt, uuid)
    rt.db.execute("UPDATE servers SET host_key = NULL WHERE uuid = ?", (uuid,))
    rt.db.audit(user, "reset_host_key", "ok", "server", uuid, server["name"], server["host_key"])
    flash(request, "Host key cleared. The next connection will trust and pin the server's current key.", "info")
    return redirect(f"/servers/{uuid}#setup")


@router.post("/servers/{uuid}/metrics/test")
async def test_metrics(request: Request, uuid: str, user: str = Depends(current_user)):
    await form_with_csrf(request)
    rt = rt_of(request)
    server = _server(rt, uuid)
    key = rt.settings.get("ssh_private_key")
    if not key:
        flash(request, "Enable metrics first.", "error")
        return redirect(f"/servers/{uuid}")
    result = await poll_server(rt, server, key)
    if result:
        flash(request, f"Connected to {result.get('hostname')}: {len(result.get('containers', []))} containers found.",
              "success")
    else:
        error = rt.db.scalar("SELECT last_probe_error FROM servers WHERE uuid = ?", (uuid,))
        flash(request, f"Connection failed: {error}", "error")
    return redirect(f"/servers/{uuid}#setup")


@router.get("/servers/{uuid}/setup.sh")
async def setup_script(request: Request, uuid: str, user: str = Depends(current_user)):
    rt = rt_of(request)
    _server(rt, uuid)
    script = install_script(ensure_keypair(rt))
    return PlainTextResponse(script, headers={"Content-Disposition": 'attachment; filename="opsmender-setup.sh"'})


@router.post("/servers/{uuid}/disk")
async def analyze_disk(request: Request, uuid: str, user: str = Depends(current_user)):
    await form_with_csrf(request)
    rt = rt_of(request)
    server = _server(rt, uuid)
    try:
        report = await disk.collect(rt, server)
    except ssh.ProbeError as exc:
        flash(request, f"Disk analysis failed: {exc}", "error")
        return redirect(f"/servers/{uuid}#disk")
    low = report["low_risk_reclaimable"]
    flash(request, (f"Analysis done. Up to {disk.human(low)} can be freed with low-risk cleanups. "
                    "Nothing was deleted.") if low else "Analysis done. Nothing was deleted.", "success")
    return redirect(f"/servers/{uuid}#disk")
