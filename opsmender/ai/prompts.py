"""Build the evidence prompt for an incident. Everything here is already redacted."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

MAX_PROMPT_CHARS = 24_000


def _ts(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError):
        return str(value)


def incident_prompt(incident: dict[str, Any], evidence: dict[str, Any], findings: dict[str, Any],
                    resource: dict[str, Any] | None) -> str:
    parts: list[str] = ["# Incident", f"- Title: {incident['title']}", f"- Kind: {incident['kind']}",
                        f"- Severity: {incident['severity']}", f"- First seen: {_ts(incident['first_seen'])}",
                        f"- Last seen: {_ts(incident['last_seen'])} (occurrences: {incident['occurrences']})"]
    if incident.get("summary"):
        parts.append(f"- Summary: {incident['summary']}")

    if resource:
        parts += ["", "# Resource", f"- Name: {resource.get('name')} ({resource.get('kind')})",
                  f"- Status: {resource.get('status')}"]
        for key in ("fqdn", "git_repository", "git_branch"):
            if resource.get(key):
                parts.append(f"- {key}: {resource[key]}")
        raw = resource.get("raw") or {}
        for key in ("build_pack", "ports_exposes", "ports_mappings", "health_check_enabled", "health_check_path",
                    "limits_memory", "limits_cpus", "base_directory", "dockerfile_location", "start_command",
                    "install_command", "build_command", "image", "type"):
            if raw.get(key) not in (None, ""):
                parts.append(f"- {key}: {raw[key]}")

    if evidence.get("deployments"):
        parts += ["", "# Recent deployments (newest first)"]
        for d in evidence["deployments"][:5]:
            parts.append(f"- {d.get('created_at')}: {d.get('status')} commit={(d.get('commit') or '')[:10]}")

    if evidence.get("metrics"):
        m = evidence["metrics"]
        parts += ["", "# Server metrics at the time"]
        for key, label in (("cpu_pct", "CPU %"), ("mem_pct", "Memory %"), ("disk_used_pct", "Root disk %"),
                           ("load1", "Load (1m)")):
            if m.get(key) is not None:
                parts.append(f"- {label}: {m[key]:.1f}")
    if evidence.get("container"):
        c = evidence["container"]
        parts += ["", "# Container", "- " + ", ".join(f"{k}={v}" for k, v in c.items() if v not in (None, ""))]

    if findings.get("findings"):
        parts += ["", "# Rule-based findings (automatic, may be incomplete)"]
        for f in findings["findings"][:6]:
            parts.append(f"- {f['title']} ({f['matches']} matching lines). Example: {f['sample'][:300]}")
    if findings.get("error_groups"):
        parts += ["", "# Distinct error lines (deduplicated, with counts)"]
        for g in findings["error_groups"][:10]:
            parts.append(f"- x{g['count']}: {g['sample'][:300]}")
            for ctx in g.get("context", [])[:3]:
                parts.append(f"    {ctx[:200]}")

    header = "\n".join(parts)
    logs = evidence.get("logs") or ""
    if logs:
        budget = MAX_PROMPT_CHARS - len(header) - 200
        if budget > 500:
            excerpt = logs[-budget:]
            header += f"\n\n# Log excerpt ({evidence.get('log_source', 'logs')}, most recent last)\n```\n{excerpt}\n```"
    return header[:MAX_PROMPT_CHARS]
