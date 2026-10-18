"""Sync Coolify inventory, detect status changes, and track deployments to completion."""

from __future__ import annotations

import logging
import time
from typing import Any

from ..adapters.base import Deployment, health_of
from ..adapters.coolify import CoolifyClient, CoolifyError, deployment_log_text
from ..db import dumps
from ..diagnosis.logs import bound
from ..diagnosis.redact import redact
from ..incidents import open_incident, resolve_incident
from ..runtime import Runtime

log = logging.getLogger("opsmender.sync")

FINAL_OK = {"finished", "success", "succeeded"}
FINAL_FAILED = {"failed", "error"}
FINAL_OTHER = {"cancelled", "cancelled-by-user", "canceled"}


async def sync(rt: Runtime) -> None:
    client = rt.coolify()
    if client is None:
        return
    try:
        if not rt.status.get("coolify_version"):
            try:
                rt.status["coolify_version"] = (await client.version()).strip('"')
            except CoolifyError:
                pass
        await _sync_servers(rt, client)
        await _sync_resources(rt, client)
        await _sync_deployments(rt, client)
        rt.status["last_sync_error"] = None
    except CoolifyError as exc:
        if rt.status.get("last_sync_error") != str(exc):
            rt.db.add_event("sync_error", str(exc), "error")
        rt.status["last_sync_error"] = str(exc)
        raise
    finally:
        rt.status["last_sync_at"] = time.time()


async def _sync_servers(rt: Runtime, client: CoolifyClient) -> None:
    now = time.time()
    servers = await client.list_servers()
    server_map: dict[str, str] = {}
    for s in servers:
        rt.db.execute(
            """INSERT INTO servers (uuid, name, ip, port, ssh_user, reachable, usable, raw, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(uuid) DO UPDATE SET name=excluded.name, ip=excluded.ip, port=excluded.port,
                 ssh_user=excluded.ssh_user, reachable=excluded.reachable, usable=excluded.usable,
                 raw=excluded.raw, updated_at=excluded.updated_at""",
            (s.uuid, s.name, s.ip, s.port, s.user, _b(s.reachable), _b(s.usable), dumps(_slim_server(s.raw)), now),
        )
        if s.reachable is False:
            open_incident(rt, fingerprint=f"server-unreachable:{s.uuid}", kind="server_unreachable",
                          severity="critical", title=f"Coolify cannot reach server {s.name}",
                          summary="Coolify reports this server as unreachable over SSH.", server_uuid=s.uuid,
                          enrich_now=False)
        elif s.reachable:
            resolve_incident(rt, f"server-unreachable:{s.uuid}", "Coolify reaches it again")
        try:
            for uuid in await client.server_resource_uuids(s.uuid):
                server_map[uuid] = s.uuid
        except CoolifyError as exc:
            log.debug("resources of server %s unavailable: %s", s.name, exc)
    known = {s.uuid for s in servers}
    for row in rt.db.all("SELECT uuid FROM servers"):
        if row["uuid"] not in known:
            rt.db.execute("DELETE FROM servers WHERE uuid = ? AND metrics_enabled = 0", (row["uuid"],))
    rt.server_map = server_map


async def _sync_resources(rt: Runtime, client: CoolifyClient) -> None:
    now = time.time()
    resources = await client.list_resources()
    previous = {r["uuid"]: r for r in rt.db.all("SELECT uuid, status, name, missing FROM resources")}
    first_sync = not previous
    seen = set()
    server_map = rt.server_map
    for r in resources:
        seen.add(r.uuid)
        server_uuid = r.server_uuid or server_map.get(r.uuid)
        prev = previous.get(r.uuid)
        changed = prev is not None and prev["status"] != r.status
        rt.db.execute(
            """INSERT INTO resources (uuid, kind, name, status, server_uuid, fqdn, git_repository, git_branch, raw,
                 updated_at, status_changed_at, missing) VALUES (?,?,?,?,?,?,?,?,?,?,?,0)
               ON CONFLICT(uuid) DO UPDATE SET kind=excluded.kind, name=excluded.name, status=excluded.status,
                 server_uuid=COALESCE(excluded.server_uuid, resources.server_uuid), fqdn=excluded.fqdn,
                 git_repository=excluded.git_repository, git_branch=excluded.git_branch, raw=excluded.raw,
                 updated_at=excluded.updated_at, missing=0,
                 status_changed_at=CASE WHEN resources.status IS NOT excluded.status THEN excluded.updated_at
                                        ELSE resources.status_changed_at END""",
            (r.uuid, r.kind, r.name, r.status, server_uuid, r.fqdn, r.git_repository, r.git_branch,
             dumps(r.raw), now, now),
        )
        if changed:
            rt.db.add_event("status_changed", f"{r.name}: {prev['status'] or 'unknown'} → {r.status or 'unknown'}",
                            "info", r.uuid)
        if first_sync or prev is None:
            continue
        evaluate_status(rt, {"uuid": r.uuid, "kind": r.kind, "name": r.name, "server_uuid": server_uuid},
                        prev["status"], r.status)
    for uuid, prev in previous.items():
        if uuid not in seen and not prev["missing"]:
            rt.db.execute("UPDATE resources SET missing = 1 WHERE uuid = ?", (uuid,))
            rt.db.add_event("resource_removed", f"{prev['name']} is no longer listed by Coolify", "info", uuid)


def evaluate_status(rt: Runtime, resource: dict[str, Any], old: str | None, new: str | None) -> None:
    before, after = health_of(old), health_of(new)
    uuid, name = resource["uuid"], resource["name"]
    if after == "ok":
        resolve_incident(rt, f"down:{uuid}", f"now {new}")
        resolve_incident(rt, f"unhealthy:{uuid}", f"now {new}")
        return
    if after in {"down", "stopped"} and before in {"ok", "warning", "unknown"} and before != after:
        if rt.is_suppressed(uuid):
            return
        verb = "is restarting" if after == "down" else "stopped"
        open_incident(rt, fingerprint=f"down:{uuid}", kind="resource_down", severity="critical",
                      title=f"{name} {verb}", summary=f"Status changed from {old or 'unknown'} to {new}.",
                      resource=resource)
    elif after == "warning" and before == "ok":
        open_incident(rt, fingerprint=f"unhealthy:{uuid}", kind="resource_unhealthy", severity="warning",
                      title=f"{name} is unhealthy", summary=f"Status changed from {old} to {new}.",
                      resource=resource)


async def _sync_deployments(rt: Runtime, client: CoolifyClient) -> None:
    now = time.time()
    running = await client.running_deployments()
    running_ids = set()
    for d in running:
        running_ids.add(d.uuid)
        app_uuid = d.app_uuid or _app_uuid_by_name(rt, d.app_name)
        existing = rt.db.one("SELECT uuid FROM deployments WHERE uuid = ?", (d.uuid,))
        rt.db.execute(
            """INSERT INTO deployments (uuid, app_uuid, app_name, status, commit_sha, created_at, updated_at, seen_at, final)
               VALUES (?,?,?,?,?,?,?,?,0)
               ON CONFLICT(uuid) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at,
                 app_uuid=COALESCE(excluded.app_uuid, deployments.app_uuid)""",
            (d.uuid, app_uuid, d.app_name, d.status, d.commit, d.created_at, d.updated_at, now),
        )
        if not existing:
            rt.db.add_event("deployment_started", f"Deployment started: {d.app_name or app_uuid}", "info", app_uuid)

    # Deployments we saw running that are no longer running have finished one way or another.
    for row in rt.db.all("SELECT * FROM deployments WHERE final = 0"):
        if row["uuid"] in running_ids:
            continue
        deployment = await client.get_deployment(row["uuid"])
        if deployment is None:
            rt.db.execute("UPDATE deployments SET final = 1, status = 'unknown' WHERE uuid = ?", (row["uuid"],))
            continue
        status = deployment.status.lower()
        if status not in FINAL_OK | FINAL_FAILED | FINAL_OTHER:
            continue  # still queued/in progress but not reported as running yet
        rt.db.execute("UPDATE deployments SET status = ?, updated_at = ?, final = 1 WHERE uuid = ?",
                      (deployment.status, deployment.updated_at, row["uuid"]))
        handle_finished_deployment(rt, row, deployment)


def handle_finished_deployment(rt: Runtime, row: dict[str, Any], deployment: Deployment) -> None:
    app_uuid = row["app_uuid"] or deployment.app_uuid
    app_name = row["app_name"] or deployment.app_name or app_uuid or "application"
    resource = rt.db.one("SELECT uuid, kind, name, server_uuid FROM resources WHERE uuid = ?", (app_uuid,)) if app_uuid else None
    status = deployment.status.lower()
    if status in FINAL_FAILED:
        logs = deployment_log_text(deployment.logs)
        if not logs.strip():
            logs = deployment_log_text(deployment.logs, include_hidden=True)
        evidence = {
            "logs": redact(bound(logs, rt.settings.int("log_lines") * 2, rt.settings.int("log_max_bytes"))),
            "log_source": "Coolify deployment log",
            "deployment_uuid": deployment.uuid,
        }
        open_incident(rt, fingerprint=f"deploy:{app_uuid or deployment.uuid}", kind="deployment_failed",
                      severity="critical", title=f"Deployment of {app_name} failed",
                      summary=f"Commit {(deployment.commit or row.get('commit_sha') or '')[:10] or 'unknown'}",
                      resource=resource, evidence=evidence)
    elif status in FINAL_OK:
        rt.db.add_event("deployment_finished", f"Deployment of {app_name} succeeded", "info", app_uuid)
        resolve_incident(rt, f"deploy:{app_uuid or deployment.uuid}", "a later deployment succeeded")
    else:
        rt.db.add_event("deployment_cancelled", f"Deployment of {app_name} was cancelled", "info", app_uuid)


async def backfill_deployments(rt: Runtime, max_apps: int = 25, per_app: int = 3) -> None:
    """On first connection, load a little deployment history so the UI isn't empty."""
    client = rt.coolify()
    if client is None or rt.db.scalar("SELECT COUNT(*) FROM deployments"):
        return
    apps = rt.db.all("SELECT uuid, name FROM resources WHERE kind = 'application' AND missing = 0 LIMIT ?", (max_apps,))
    now = time.time()
    for app in apps:
        try:
            deployments = await client.app_deployments(app["uuid"], take=per_app)
        except CoolifyError:
            continue
        for d in deployments:
            final = 1 if d.status.lower() in FINAL_OK | FINAL_FAILED | FINAL_OTHER else 0
            rt.db.execute(
                "INSERT OR IGNORE INTO deployments (uuid, app_uuid, app_name, status, commit_sha, created_at,"
                " updated_at, seen_at, final) VALUES (?,?,?,?,?,?,?,?,?)",
                (d.uuid, app["uuid"], app["name"], d.status, d.commit, d.created_at, d.updated_at, now, final),
            )


def _app_uuid_by_name(rt: Runtime, name: str | None) -> str | None:
    if not name:
        return None
    row = rt.db.one("SELECT uuid FROM resources WHERE kind = 'application' AND name = ?", (name,))
    return row["uuid"] if row else None


def _slim_server(raw: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in raw.items() if not isinstance(v, (dict, list)) and "key" not in k and "secret" not in k}


def _b(value: bool | None) -> int | None:
    return None if value is None else int(value)
