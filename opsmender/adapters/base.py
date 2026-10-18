"""Provider-neutral shapes. Coolify is the first adapter; Docker or Kubernetes can follow."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Server:
    uuid: str
    name: str
    ip: str | None = None
    port: int | None = None
    user: str | None = None
    reachable: bool | None = None
    usable: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Resource:
    uuid: str
    kind: str  # application | service | database
    name: str
    status: str | None = None
    server_uuid: str | None = None
    fqdn: str | None = None
    git_repository: str | None = None
    git_branch: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Deployment:
    uuid: str
    status: str
    app_uuid: str | None = None
    app_name: str | None = None
    commit: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    logs: list[dict[str, Any]] = field(default_factory=list)


class Provider(Protocol):
    async def list_servers(self) -> list[Server]: ...
    async def list_resources(self) -> list[Resource]: ...
    async def running_deployments(self) -> list[Deployment]: ...
    async def get_deployment(self, uuid: str) -> Deployment | None: ...
    async def app_deployments(self, app_uuid: str, take: int = 10) -> list[Deployment]: ...
    async def resource_logs(self, resource: Resource, lines: int) -> str | None: ...
    async def perform(self, kind: str, uuid: str, action: str) -> str: ...


def health_of(status: str | None) -> str:
    """Collapse provider status strings into ok | warning | down | stopped | unknown."""
    if not status:
        return "unknown"
    base, _, health = status.lower().partition(":")
    base = base.strip()
    health = health.strip()
    if base in {"running", "healthy"}:
        return "warning" if health == "unhealthy" else "ok"
    if base.startswith("degraded") or base == "unhealthy":
        return "warning"
    if base.startswith("restarting") or base in {"dead", "crashloop"}:
        return "down"
    if base in {"exited", "stopped", "removing", "paused"}:
        return "stopped"
    return "unknown"
