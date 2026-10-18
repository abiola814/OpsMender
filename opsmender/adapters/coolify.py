"""Async client for the Coolify v4 REST API (``/api/v1``).

Field names differ between Coolify releases, so every parser reads defensively and never assumes
a key exists. Endpoints used:

    GET  /api/v1/version
    GET  /api/v1/servers, /servers/{uuid}/resources
    GET  /api/v1/applications, /services, /databases
    GET  /api/v1/applications/{uuid}/logs?lines=N
    GET  /api/v1/deployments                       (currently running)
    GET  /api/v1/deployments/{uuid}
    GET  /api/v1/deployments/applications/{uuid}
    GET|POST /api/v1/{applications|services|databases}/{uuid}/{start|stop|restart}
    GET  /api/v1/deploy?uuid=...
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .base import Deployment, Resource, Server

KIND_PATHS = {"application": "applications", "service": "services", "database": "databases"}
ACTIONS = {"start", "stop", "restart", "redeploy"}
_APP_UUID_IN_URL = re.compile(r"/application/([A-Za-z0-9]+)")


class CoolifyError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class CoolifyClient:
    def __init__(self, base_url: str, token: str, verify_tls: bool = True, timeout: float = 20.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url + "/api/v1",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            verify=verify_tls,
            timeout=timeout,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise CoolifyError(f"Cannot reach Coolify at {self.base_url}: {exc}") from exc
        if resp.status_code == 401:
            raise CoolifyError("Coolify rejected the API token (401).", 401)
        if resp.status_code == 403:
            raise CoolifyError("The API token lacks permission for this request (403).", 403)
        if resp.status_code >= 400:
            message = _error_message(resp)
            raise CoolifyError(f"Coolify {method} {path} failed ({resp.status_code}): {message}", resp.status_code)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    async def _get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", path, params={k: v for k, v in params.items() if v is not None})

    # Discovery

    async def version(self) -> str:
        data = await self._get("/version")
        return data if isinstance(data, str) else json.dumps(data)

    async def list_servers(self) -> list[Server]:
        servers = []
        for item in _as_list(await self._get("/servers")):
            settings = item.get("settings") or {}
            servers.append(Server(
                uuid=str(item.get("uuid")),
                name=item.get("name") or item.get("uuid") or "server",
                ip=item.get("ip"),
                port=_int(item.get("port")),
                user=item.get("user"),
                reachable=_bool(settings.get("is_reachable", item.get("is_reachable"))),
                usable=_bool(settings.get("is_usable", item.get("is_usable"))),
                raw=item,
            ))
        return servers

    async def server_resource_uuids(self, server_uuid: str) -> list[str]:
        data = await self._get(f"/servers/{server_uuid}/resources")
        return [str(r.get("uuid")) for r in _as_list(data) if r.get("uuid")]

    async def list_resources(self) -> list[Resource]:
        resources: list[Resource] = []
        for kind, path in KIND_PATHS.items():
            try:
                items = _as_list(await self._get(f"/{path}"))
            except CoolifyError as exc:
                if exc.status == 404:
                    continue
                raise
            for item in items:
                if not item.get("uuid"):
                    continue
                resources.append(Resource(
                    uuid=str(item["uuid"]),
                    kind=kind,
                    name=item.get("name") or item["uuid"],
                    status=item.get("status"),
                    server_uuid=_server_uuid_of(item),
                    fqdn=item.get("fqdn"),
                    git_repository=item.get("git_repository"),
                    git_branch=item.get("git_branch"),
                    raw=_slim(item),
                ))
        return resources

    # Deployments

    async def running_deployments(self) -> list[Deployment]:
        return [_deployment(d) for d in _as_list(await self._get("/deployments")) if _deployment_uuid(d)]

    async def get_deployment(self, uuid: str) -> Deployment | None:
        try:
            data = await self._get(f"/deployments/{uuid}")
        except CoolifyError as exc:
            if exc.status == 404:
                return None
            raise
        return _deployment(data) if isinstance(data, dict) else None

    async def app_deployments(self, app_uuid: str, take: int = 10) -> list[Deployment]:
        data = await self._get(f"/deployments/applications/{app_uuid}", skip=0, take=take)
        if isinstance(data, dict):
            data = data.get("deployments", data.get("data", []))
        deployments = [_deployment(d) for d in _as_list(data) if _deployment_uuid(d)]
        for d in deployments:
            d.app_uuid = d.app_uuid or app_uuid
        return deployments

    # Logs

    async def resource_logs(self, resource: Resource, lines: int) -> str | None:
        """Recent container logs. Coolify exposes this for applications only."""
        if resource.kind != "application":
            return None
        data = await self._get(f"/applications/{resource.uuid}/logs", lines=lines)
        if isinstance(data, dict):
            logs = data.get("logs", "")
            return "\n".join(logs) if isinstance(logs, list) else str(logs)
        return str(data) if data else ""

    # Actions

    async def perform(self, kind: str, uuid: str, action: str) -> str:
        if action not in ACTIONS:
            raise CoolifyError(f"Unsupported action {action!r}")
        if action == "redeploy":
            data = await self._get("/deploy", uuid=uuid, force="false")
            return _message(data) or "Deployment queued."
        path = f"/{KIND_PATHS[kind]}/{uuid}/{action}"
        try:
            data = await self._request("POST", path)
        except CoolifyError as exc:
            if exc.status != 405:
                raise
            data = await self._request("GET", path)
        return _message(data) or f"{action.capitalize()} requested."


def _deployment_uuid(d: dict[str, Any]) -> str | None:
    return d.get("deployment_uuid") or d.get("uuid")


def _deployment(d: dict[str, Any]) -> Deployment:
    app_uuid = d.get("application_uuid")
    if not app_uuid:
        match = _APP_UUID_IN_URL.search(str(d.get("deployment_url") or ""))
        app_uuid = match.group(1) if match else None
    return Deployment(
        uuid=str(_deployment_uuid(d)),
        status=str(d.get("status") or "unknown"),
        app_uuid=app_uuid,
        app_name=d.get("application_name"),
        commit=d.get("commit"),
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
        logs=parse_deployment_logs(d.get("logs")),
    )


def parse_deployment_logs(raw: Any) -> list[dict[str, Any]]:
    """Coolify stores build output as a JSON array (often JSON-encoded inside a string)."""
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return [{"output": line, "type": "stdout"} for line in raw.splitlines()]
    if not isinstance(raw, list):
        return []
    entries = []
    for item in raw:
        if isinstance(item, dict):
            entries.append({
                "output": str(item.get("output", "")),
                "type": item.get("type", "stdout"),
                "hidden": bool(item.get("hidden", False)),
                "timestamp": item.get("timestamp"),
            })
        else:
            entries.append({"output": str(item), "type": "stdout"})
    return entries


def deployment_log_text(entries: list[dict[str, Any]], include_hidden: bool = False) -> str:
    return "\n".join(e["output"] for e in entries if include_hidden or not e.get("hidden"))


def _server_uuid_of(item: dict[str, Any]) -> str | None:
    for path in (("destination", "server", "uuid"), ("server", "uuid")):
        node: Any = item
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if node:
            return str(node)
    return item.get("server_uuid")


def _slim(item: dict[str, Any]) -> dict[str, Any]:
    """Drop fields that may hold secrets or bulky config before storing the raw record."""
    drop = {"environment_variables", "dockerfile", "docker_compose", "docker_compose_raw",
            "private_key", "manual_webhook_secret_github", "manual_webhook_secret_gitlab",
            "manual_webhook_secret_bitbucket", "manual_webhook_secret_gitea", "postgres_password",
            "mysql_password", "mysql_root_password", "mariadb_password", "mariadb_root_password",
            "mongo_initdb_root_password", "redis_password", "keydb_password", "dragonfly_password",
            "clickhouse_admin_password", "internal_db_url", "external_db_url"}
    return {k: v for k, v in item.items()
            if k not in drop and "password" not in k and "secret" not in k and not isinstance(v, (list, dict))}


def _as_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        data = data.get("data", [data] if data.get("uuid") else [])
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _message(data: Any) -> str | None:
    if isinstance(data, dict):
        msg = data.get("message")
        if not msg and isinstance(data.get("deployments"), list):
            msg = "; ".join(str(d.get("message", "")) for d in data["deployments"] if isinstance(d, dict))
        return msg or None
    return str(data) if data else None


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        return str(data.get("message") or data.get("error") or data)[:300]
    except ValueError:
        return resp.text[:300]


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes"}
    return bool(value)
