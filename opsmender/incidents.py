"""Incident lifecycle: open (deduplicated by fingerprint), enrich with evidence, diagnose, resolve."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Coroutine

from .adapters.base import Resource
from .ai.prompts import incident_prompt
from .ai.providers import AIConfig, AIError, PRESETS, complete
from .db import dumps, loads
from .diagnosis.logs import bound
from .diagnosis.redact import redact
from .diagnosis.rules import analyze
from .metrics import disk, ssh
from .notify import send
from .runtime import Runtime

log = logging.getLogger("opsmender.incidents")
DISK_KINDS = {"disk_pressure", "disk_forecast"}
_tasks: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> None:
    """Fire-and-forget a coroutine while keeping a reference so it isn't garbage collected."""
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def open_incident(rt: Runtime, *, fingerprint: str, kind: str, severity: str, title: str,
                  summary: str | None = None, resource: dict[str, Any] | None = None,
                  server_uuid: str | None = None, evidence: dict[str, Any] | None = None,
                  enrich_now: bool = True) -> tuple[int, bool]:
    now = time.time()
    existing = rt.db.one("SELECT * FROM incidents WHERE fingerprint = ? AND status = 'open'", (fingerprint,))
    if existing:
        rt.db.execute("UPDATE incidents SET last_seen = ?, occurrences = occurrences + 1 WHERE id = ?",
                      (now, existing["id"]))
        return existing["id"], False

    cur = rt.db.execute(
        "INSERT INTO incidents (fingerprint, kind, severity, title, summary, resource_kind, resource_uuid,"
        " resource_name, server_uuid, first_seen, last_seen, evidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (fingerprint, kind, severity, title, summary,
         (resource or {}).get("kind"), (resource or {}).get("uuid"), (resource or {}).get("name"),
         server_uuid or (resource or {}).get("server_uuid"), now, now, dumps(evidence or {})),
    )
    incident_id = int(cur.lastrowid)
    level = "error" if severity == "critical" else "warning"
    rt.db.add_event("incident_opened", title, level, (resource or {}).get("uuid"), incident_id)
    spawn(send(rt.settings.get("notify_webhook_url"), f"🔴 OpsMender: {title}" + (f"\n{summary}" if summary else "")))
    if enrich_now:
        spawn(enrich(rt, incident_id))
    return incident_id, True


def resolve_incident(rt: Runtime, fingerprint: str, note: str | None = None) -> bool:
    row = rt.db.one("SELECT * FROM incidents WHERE fingerprint = ? AND status = 'open'", (fingerprint,))
    if not row:
        return False
    rt.db.execute("UPDATE incidents SET status = 'resolved', resolved_at = ? WHERE id = ?", (time.time(), row["id"]))
    message = f"Resolved: {row['title']}" + (f" ({note})" if note else "")
    rt.db.add_event("incident_resolved", message, "info", row["resource_uuid"], row["id"])
    if rt.settings.bool("notify_on_resolve"):
        spawn(send(rt.settings.get("notify_webhook_url"), f"✅ OpsMender: {message}"))
    return True


def open_fingerprints(rt: Runtime, prefix: str) -> set[str]:
    rows = rt.db.all("SELECT fingerprint FROM incidents WHERE status = 'open' AND fingerprint LIKE ?", (prefix + "%",))
    return {r["fingerprint"] for r in rows}


def resource_row(rt: Runtime, uuid: str | None) -> dict[str, Any] | None:
    if not uuid:
        return None
    row = rt.db.one("SELECT * FROM resources WHERE uuid = ?", (uuid,))
    if row:
        row["raw"] = loads(row.get("raw"), {})
    return row


def container_for(rt: Runtime, resource: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Find the container belonging to a Coolify resource (container names include the resource uuid)."""
    servers = rt.db.all("SELECT * FROM servers WHERE metrics_enabled = 1")
    if resource.get("server_uuid"):
        servers.sort(key=lambda s: s["uuid"] != resource["server_uuid"])
    for server in servers:
        current = loads(server.get("current_metrics"), {}) or {}
        matches = [c for c in current.get("containers", []) if resource["uuid"] in (c.get("name") or "")]
        if matches:
            matches.sort(key=lambda c: c.get("state") != "running")
            return server, matches[0]
    return None, None


async def fetch_logs(rt: Runtime, resource: dict[str, Any]) -> tuple[str, str]:
    """Recent logs for a resource, bounded and redacted. Returns (text, source)."""
    lines = rt.settings.int("log_lines")
    max_bytes = rt.settings.int("log_max_bytes")
    text, source = "", ""
    client = rt.coolify()
    if client and resource.get("kind") == "application":
        try:
            text = await client.resource_logs(Resource(uuid=resource["uuid"], kind="application",
                                                       name=resource.get("name", "")), lines) or ""
            source = "Coolify application logs"
        except Exception as exc:  # noqa: BLE001 - fall through to the probe
            log.info("Coolify logs unavailable for %s: %s", resource.get("name"), exc)
    if not text.strip():
        server, container = container_for(rt, resource)
        key = rt.settings.get("ssh_private_key")
        if server and container and key:
            try:
                text = await ssh.container_logs(server["probe_host"] or server["ip"], server["probe_port"] or 22,
                                                key, server["host_key"], container["name"], lines)
                source = f"docker logs {container['name']} (via probe)"
            except ssh.ProbeError as exc:
                log.info("probe logs unavailable for %s: %s", container["name"], exc)
    return redact(bound(text, lines, max_bytes)), source


async def enrich(rt: Runtime, incident_id: int) -> None:
    inc = rt.db.one("SELECT * FROM incidents WHERE id = ?", (incident_id,))
    if not inc:
        return
    evidence = loads(inc["evidence"], {}) or {}
    resource = resource_row(rt, inc["resource_uuid"])
    try:
        if resource and not evidence.get("logs"):
            evidence["logs"], evidence["log_source"] = await fetch_logs(rt, resource)
        if resource and resource["kind"] == "application" and "deployments" not in evidence:
            evidence["deployments"] = rt.db.all(
                'SELECT uuid, status, commit_sha AS "commit", created_at FROM deployments WHERE app_uuid = ?'
                " ORDER BY created_at DESC LIMIT 5", (resource["uuid"],))
        server_uuid = inc["server_uuid"] or (resource or {}).get("server_uuid")
        if server_uuid and "metrics" not in evidence:
            server = rt.db.one("SELECT current_metrics FROM servers WHERE uuid = ?", (server_uuid,))
            current = loads((server or {}).get("current_metrics"), {}) or {}
            if current:
                evidence["metrics"] = {k: current.get(k) for k in ("cpu_pct", "mem_pct", "disk_used_pct", "load1")}
        if resource and "container" not in evidence:
            _, container = container_for(rt, resource)
            if container:
                evidence["container"] = {k: container.get(k) for k in
                                         ("name", "state", "health", "restarts", "oom_killed", "exit_code", "cpu_pct")}
                if container.get("mem_bytes"):
                    evidence["container"]["mem_mb"] = round(container["mem_bytes"] / 1_048_576)
        if inc["kind"] in DISK_KINDS and server_uuid and "disk" not in evidence:
            server = rt.db.one("SELECT * FROM servers WHERE uuid = ?", (server_uuid,))
            if server and server["metrics_enabled"]:
                try:
                    evidence["disk"] = await disk.collect(rt, server)
                    evidence.pop("disk_error", None)
                except ssh.ProbeError as exc:
                    evidence["disk_error"] = str(exc)
    except Exception:  # noqa: BLE001 - evidence is best effort
        log.exception("collecting evidence for incident %s failed", incident_id)

    findings = analyze(evidence.get("logs") or "") if evidence.get("logs") else {"findings": [], "error_groups": []}
    if evidence.get("disk"):
        disk_findings = disk.findings(evidence["disk"])
        findings["findings"] = disk_findings + findings["findings"]
        findings["headline"] = findings.get("headline") or (disk_findings[0]["title"] if disk_findings else None)
    summary = inc["summary"]
    if not summary and findings.get("headline"):
        summary = findings["headline"]
    if inc["kind"] in DISK_KINDS:
        report = evidence.get("disk")
        if report and report.get("low_risk_reclaimable"):
            summary = (f"Up to {disk.human(report['low_risk_reclaimable'])} can be freed with low-risk cleanups. "
                       "Nothing has been deleted.")
        elif report:
            summary = findings.get("headline") or "No large low-risk cleanup found; see the breakdown below."
        elif evidence.get("disk_error"):
            summary = f"Couldn't analyze disk usage: {evidence['disk_error']}"
    rt.db.execute("UPDATE incidents SET evidence = ?, findings = ?, summary = ? WHERE id = ?",
                  (dumps(evidence), dumps(findings), summary, incident_id))

    if rt.settings.bool("ai_auto_diagnose") and ai_config(rt):
        try:
            await diagnose(rt, incident_id)
        except AIError as exc:
            rt.db.add_event("ai_error", f"AI diagnosis failed: {exc}", "warning", inc["resource_uuid"], incident_id)


def ai_config(rt: Runtime) -> AIConfig | None:
    provider = rt.settings.get("ai_provider") or ""
    if not provider:
        return None
    preset = PRESETS.get(provider, PRESETS["custom"])
    return AIConfig(provider=provider, model=rt.settings.get("ai_model") or preset.default_model,
                    api_key=rt.settings.get("ai_api_key"), base_url=rt.settings.get("ai_base_url") or None)


async def diagnose(rt: Runtime, incident_id: int) -> str:
    cfg = ai_config(rt)
    if not cfg:
        raise AIError("No AI provider configured. Add one in Settings.")
    if incident_id in rt.ai_inflight:
        raise AIError("A diagnosis for this incident is already running.")
    inc = rt.db.one("SELECT * FROM incidents WHERE id = ?", (incident_id,))
    if not inc:
        raise AIError("Incident not found.")
    rt.ai_inflight.add(incident_id)
    try:
        prompt = incident_prompt(inc, loads(inc["evidence"], {}) or {}, loads(inc["findings"], {}) or {},
                                 resource_row(rt, inc["resource_uuid"]))
        answer = await complete(cfg, prompt)
    finally:
        rt.ai_inflight.discard(incident_id)
    rt.db.execute("UPDATE incidents SET ai_diagnosis = ?, ai_model = ?, ai_at = ? WHERE id = ?",
                  (answer, f"{cfg.preset.label} · {cfg.model}", time.time(), incident_id))
    rt.db.add_event("ai_diagnosis", f"AI diagnosis ready: {inc['title']}", "info", inc["resource_uuid"], incident_id)
    return answer
