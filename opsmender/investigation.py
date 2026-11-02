"""Read-only tools the AI can call while investigating an incident.

Every tool reads what OpsMender already has access to (its database, the Coolify API, the probe) and
nothing changes anything. Output is redacted and capped before it goes back to the model.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .adapters.base import health_of
from .adapters.coolify import CoolifyError, deployment_log_text
from .ai.prompts import CONFIG_KEYS
from .ai.providers import Tool
from .db import loads
from .diagnosis.logs import bound
from .diagnosis.redact import redact
from .incidents import container_for, fetch_logs, resource_row
from .metrics import disk, ssh, store
from .runtime import Runtime

MAX_OUTPUT = 6000
DISK_REPORT_MAX_AGE = 15 * 60
_DEPLOYMENT_UUID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_RESOURCE = {"type": "string", "description": "Resource name or uuid. Defaults to the incident's resource."}
_SERVER = {"type": "string", "description": "Server name or uuid. Defaults to the incident's server."}


def _schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


TOOLS = [
    Tool("get_logs", "Recent runtime logs of an application, service or database (redacted, newest last). "
         "Use when the log excerpt is missing, too short, or from a different resource than the one you suspect.",
         _schema({"resource": _RESOURCE,
                  "lines": {"type": "integer", "minimum": 20, "maximum": 500, "description": "Default 200."}})),
    Tool("get_deployments", "The last 10 deployments of an application: uuid, status, commit and time.",
         _schema({"resource": _RESOURCE})),
    Tool("get_deployment_log", "Build and deploy log of one deployment (redacted, last 400 lines). Use the uuid "
         "from get_deployments, for example to compare a failed deployment with the last good one.",
         _schema({"deployment_uuid": {"type": "string"}}, ("deployment_uuid",))),
    Tool("get_resource", "Coolify configuration of a resource: build pack, ports, health check, resource limits, "
         "start/build commands, image, domain, repository, current status and since when.",
         _schema({"resource": _RESOURCE})),
    Tool("get_env_var_names", "Names of the environment variables set in Coolify for an application or service. "
         "Values are never read. Use it to check whether a variable the logs complain about exists.",
         _schema({"resource": _RESOURCE})),
    Tool("get_server_metrics", "A server's CPU, memory, disk and load: latest values plus min/avg/max over the "
         "last hours, and the containers using the most CPU and memory right now.",
         _schema({"server": _SERVER,
                  "hours": {"type": "integer", "minimum": 1, "maximum": 24, "description": "Default 6."}})),
    Tool("get_container_stats", "The container behind a resource: state, health, restarts, exit code, OOM kills, "
         "memory limit, and its CPU/memory/restart history over the last hours.",
         _schema({"resource": _RESOURCE,
                  "hours": {"type": "integer", "minimum": 1, "maximum": 6, "description": "Default 6."}})),
    Tool("get_disk_usage", "What is using a server's disk: Docker images, build cache, volumes, container logs, "
         "journal, Coolify backups, each with reclaimable size and risk. Read-only.",
         _schema({"server": _SERVER})),
    Tool("list_resources", "Every application, service and database OpsMender knows, with status and server. "
         "Use it to find a related resource, such as the database an app connects to.",
         _schema({"server": _SERVER, "problems_only": {"type": "boolean", "description": "Default false."}})),
    Tool("get_history", "Past incidents and recent events (status changes, deployments, actions) for a resource.",
         _schema({"resource": _RESOURCE,
                  "days": {"type": "integer", "minimum": 1, "maximum": 30, "description": "Default 7."}})),
]


class ToolError(Exception):
    """A problem the model can act on, returned to it as an error result."""


class Toolbox:
    """Runs the model's tool calls for one incident."""

    def __init__(self, rt: Runtime, incident: dict[str, Any]):
        self.rt = rt
        self.incident = incident
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {
            t.name: getattr(self, t.name) for t in TOOLS}

    async def run(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        handler = self._handlers.get(name)
        if not handler:
            return f"Unknown tool {name!r}. Available: {', '.join(self._handlers)}.", True
        try:
            result = await handler(args)
        except ToolError as exc:
            return str(exc), True
        except CoolifyError as exc:
            return f"Coolify: {exc}", True
        except ssh.ProbeError as exc:
            return f"Probe: {exc}", True
        if isinstance(result, str):
            return _clip(redact(result), tail=True), False
        return _clip(redact(json.dumps(result, default=str, separators=(",", ":")))), False

    # Lookups

    def _resource(self, ref: Any) -> dict[str, Any]:
        ref = str(ref or "").strip() or self.incident.get("resource_uuid") or ""
        if not ref:
            raise ToolError("This incident isn't tied to a resource. Pass one; list_resources shows them all.")
        row = resource_row(self.rt, ref)
        if row:
            return row
        rows = self.rt.db.all("SELECT uuid FROM resources WHERE lower(name) = lower(?) AND missing = 0", (ref,))
        if len(rows) == 1:
            return resource_row(self.rt, rows[0]["uuid"])  # type: ignore[return-value]
        if rows:
            raise ToolError(f"Several resources are named {ref!r}: {', '.join(r['uuid'] for r in rows)}. "
                            "Pass the uuid.")
        raise ToolError(f"No resource {ref!r}. list_resources shows what exists.")

    def _server(self, ref: Any) -> dict[str, Any]:
        ref = str(ref or "").strip()
        if not ref:
            ref = self.incident.get("server_uuid") or (resource_row(self.rt, self.incident.get("resource_uuid"))
                                                       or {}).get("server_uuid") or ""
        if not ref:
            raise ToolError("This incident isn't tied to a server. Pass one by name or uuid.")
        row = self.rt.db.one("SELECT * FROM servers WHERE uuid = ? OR lower(name) = lower(?)", (ref, ref))
        if not row:
            names = ", ".join(r["name"] for r in self.rt.db.all("SELECT name FROM servers ORDER BY name"))
            raise ToolError(f"No server {ref!r}. Known servers: {names or 'none'}.")
        return row

    def _coolify(self) -> Any:
        client = self.rt.coolify()
        if not client:
            raise ToolError("OpsMender isn't connected to Coolify.")
        return client

    # Tools (names match TOOLS)

    async def get_logs(self, args: dict[str, Any]) -> str:
        resource = self._resource(args.get("resource"))
        text, source = await fetch_logs(self.rt, resource, _int(args.get("lines"), 200, 20, 500))
        if not text.strip():
            return (f"No logs available for {resource['name']}: Coolify returned none and the probe couldn't "
                    "read the container (is metrics set up on its server?).")
        return f"Source: {source}\n{text}"

    async def get_deployments(self, args: dict[str, Any]) -> Any:
        resource = self._resource(args.get("resource"))
        if resource["kind"] != "application":
            raise ToolError(f"{resource['name']} is a {resource['kind']}; only applications have deployments.")
        rows = self.rt.db.all(
            'SELECT uuid, status, commit_sha AS "commit", created_at, updated_at FROM deployments'
            " WHERE app_uuid = ? ORDER BY created_at DESC LIMIT 10", (resource["uuid"],))
        if not rows:
            rows = [{"uuid": d.uuid, "status": d.status, "commit": d.commit, "created_at": d.created_at,
                     "updated_at": d.updated_at}
                    for d in await self._coolify().app_deployments(resource["uuid"], 10)]
        return {"resource": resource["name"], "deployments": rows}

    async def get_deployment_log(self, args: dict[str, Any]) -> str:
        uuid = str(args.get("deployment_uuid") or "").strip()
        if not _DEPLOYMENT_UUID.match(uuid):
            raise ToolError("deployment_uuid must be a uuid from get_deployments.")
        deployment = await self._coolify().get_deployment(uuid)
        if not deployment:
            raise ToolError(f"Coolify has no deployment {uuid}.")
        text = bound(deployment_log_text(deployment.logs), 400, self.rt.settings.int("log_max_bytes"))
        head = (f"Deployment {uuid} of {deployment.app_name or deployment.app_uuid}: status {deployment.status}, "
                f"commit {(deployment.commit or 'unknown')[:12]}, started {deployment.created_at}")
        return f"{head}\n{text or '(empty log)'}"

    async def get_resource(self, args: dict[str, Any]) -> Any:
        r = self._resource(args.get("resource"))
        server = self.rt.db.one("SELECT name FROM servers WHERE uuid = ?", (r["server_uuid"],)) if r["server_uuid"] else None
        raw = r.get("raw") or {}
        return {
            "uuid": r["uuid"], "name": r["name"], "kind": r["kind"], "status": r["status"],
            "status_since": _when(r.get("status_changed_at")), "server": (server or {}).get("name"),
            "project": r.get("project"), "environment": r.get("environment"), "fqdn": r.get("fqdn"),
            "git_repository": r.get("git_repository"), "git_branch": r.get("git_branch"),
            "config": {k: raw[k] for k in CONFIG_KEYS if raw.get(k) not in (None, "")},
        }

    async def get_env_var_names(self, args: dict[str, Any]) -> Any:
        resource = self._resource(args.get("resource"))
        if resource["kind"] == "database":
            raise ToolError(f"{resource['name']} is a database; Coolify configures it without user-defined "
                            "environment variables.")
        names = await self._coolify().env_var_names(resource["kind"], resource["uuid"])
        return {"resource": resource["name"], "names": names, "note": "Names only; values are never read."}

    async def get_server_metrics(self, args: dict[str, Any]) -> Any:
        server = self._server(args.get("server"))
        if not server["metrics_enabled"]:
            raise ToolError(f"Metrics aren't set up on {server['name']}.")
        hours = _int(args.get("hours"), 6, 1, 24)
        points = store.history(self.rt.db, server["uuid"], hours)
        current = loads(server.get("current_metrics"), {}) or {}
        containers = current.get("containers") or []
        top_cpu = sorted((c for c in containers if c.get("cpu_pct") is not None), key=lambda c: -c["cpu_pct"])[:5]
        top_mem = sorted((c for c in containers if c.get("mem_bytes")), key=lambda c: -c["mem_bytes"])[:5]
        return {
            "server": server["name"], "last_probe": _when(server.get("last_probe_at")),
            "last_probe_error": server.get("last_probe_error"),
            "now": {k: current.get(k) for k in ("cpu_pct", "mem_pct", "disk_used_pct", "load1", "swap_used")},
            f"last_{hours}h": {name: _summary([p[name] for p in points]) for name in ("cpu", "mem", "disk")},
            "top_cpu": [{"name": c["name"], "cpu_pct": round(c["cpu_pct"], 1)} for c in top_cpu],
            "top_memory": [{"name": c["name"], "mem_mb": round(c["mem_bytes"] / 1_048_576)} for c in top_mem],
        }

    async def get_container_stats(self, args: dict[str, Any]) -> Any:
        resource = self._resource(args.get("resource"))
        server, container = container_for(self.rt, resource)
        if not server or not container:
            raise ToolError(f"No container found for {resource['name']}. Metrics may not be set up on its server, "
                            "or the container no longer exists.")
        hours = _int(args.get("hours"), 6, 1, 6)
        history = store.container_history(self.rt.db, server["uuid"], container["name"], hours)
        restarts = [h["restarts"] for h in history if h["restarts"] is not None]
        return {
            "server": server["name"],
            "container": {k: v for k, v in container.items() if k not in ("id", "mem_bytes", "mem_limit")},
            "mem_mb": _mb(container.get("mem_bytes")), "mem_limit_mb": _mb(container.get("mem_limit")),
            f"last_{hours}h": {
                "cpu_pct": _summary([h["cpu_pct"] for h in history]),
                "mem_mb": _summary([_mb(h["mem_bytes"]) for h in history]),
                "restarts_added": (restarts[-1] - restarts[0]) if len(restarts) > 1 else 0,
                "samples": len(history),
            },
        }

    async def get_disk_usage(self, args: dict[str, Any]) -> Any:
        server = self._server(args.get("server"))
        fresh = server.get("disk_at") and time.time() - server["disk_at"] < DISK_REPORT_MAX_AGE
        report = disk.report_for(self.rt, server) if fresh else await disk.collect(self.rt, server)
        if not report:
            raise ToolError(f"No disk report for {server['name']}.")
        fs = report["fs"]
        return {
            "server": server["name"], "analyzed": _when(report.get("ts")),
            "filesystem": f"{fs.get('mount')}: {disk.human(fs['used'])} used of {disk.human(fs['total'])}, "
                          f"{disk.human(fs['avail'])} free",
            "docker_log_driver": report.get("log_driver"), "log_rotation": report.get("log_rotation"),
            "items": [{"what": i["label"], "size": disk.human(i.get("size")),
                       "reclaimable": disk.human(i.get("reclaimable")), "risk": i["risk"]} for i in report["items"]],
            "largest_container_logs": [{"container": e.get("name"), "size": disk.human(e.get("bytes"))}
                                       for e in report.get("top_logs", [])[:5]],
        }

    async def list_resources(self, args: dict[str, Any]) -> Any:
        where, params = "missing = 0", []
        if args.get("server"):
            where += " AND server_uuid = ?"
            params.append(self._server(args["server"])["uuid"])
        rows = self.rt.db.all(f"SELECT r.uuid, r.name, r.kind, r.status, s.name AS server FROM resources r"
                              f" LEFT JOIN servers s ON s.uuid = r.server_uuid WHERE {where} ORDER BY r.name", params)
        for r in rows:
            r["health"] = health_of(r["status"])
        if args.get("problems_only"):
            rows = [r for r in rows if r["health"] not in ("ok", "stopped")]
        rows.sort(key=lambda r: r["health"] in ("ok", "stopped"))
        return {"count": len(rows), "resources": rows[:80]}

    async def get_history(self, args: dict[str, Any]) -> Any:
        resource = self._resource(args.get("resource"))
        since = time.time() - _int(args.get("days"), 7, 1, 30) * 86400
        incidents = self.rt.db.all(
            "SELECT id, kind, title, status, first_seen, occurrences, resolved_at FROM incidents"
            " WHERE resource_uuid = ? AND last_seen >= ? ORDER BY first_seen DESC LIMIT 15", (resource["uuid"], since))
        events = self.rt.db.all("SELECT ts, kind, message FROM events WHERE resource_uuid = ? AND ts >= ?"
                                " ORDER BY ts DESC LIMIT 30", (resource["uuid"], since))
        return {
            "resource": resource["name"],
            "incidents": [{**i, "first_seen": _when(i["first_seen"]), "resolved_at": _when(i["resolved_at"]),
                           "current": i["id"] == self.incident["id"]} for i in incidents],
            "events": [{**e, "ts": _when(e["ts"])} for e in events],
        }


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _mb(value: Any) -> int | None:
    return round(value / 1_048_576) if value else None


def _when(ts: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return None


def _summary(values: list[Any]) -> dict[str, float] | None:
    values = [float(v) for v in values if v is not None]
    if not values:
        return None
    return {"min": round(min(values), 1), "avg": round(sum(values) / len(values), 1),
            "max": round(max(values), 1), "last": round(values[-1], 1)}


def _clip(text: str, tail: bool = False) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    note = f"[cut to {MAX_OUTPUT} of {len(text)} characters]"
    return f"{note}\n{text[-MAX_OUTPUT:]}" if tail else f"{text[:MAX_OUTPUT]}\n{note}"
