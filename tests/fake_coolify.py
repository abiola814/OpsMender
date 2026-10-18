"""In-memory stand-in for the Coolify API, served through httpx.MockTransport."""

from __future__ import annotations

import json

import httpx


class FakeCoolify:
    def __init__(self):
        self.token = "test-token"
        self.servers = [{"uuid": "srv1", "name": "main", "ip": "host.docker.internal", "port": 22, "user": "root",
                         "settings": {"is_reachable": True, "is_usable": True}}]
        self.apps = [{"uuid": "app1", "name": "api", "status": "running:healthy", "fqdn": "https://api.example.com",
                      "git_repository": "acme/api", "git_branch": "main", "build_pack": "nixpacks",
                      "environment_variables": [{"key": "SECRET", "value": "x"}]}]
        self.services = [{"uuid": "svc1", "name": "plausible", "status": "running:healthy"}]
        self.databases = [{"uuid": "db1", "name": "postgres", "status": "running:healthy",
                           "postgres_password": "hunter2"}]
        self.running: list[dict] = []
        self.deployments: dict[str, dict] = {}
        self.app_logs = "Server listening on 0.0.0.0:3000"
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        self.calls.append((request.method, path))
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"message": "Unauthenticated."})
        routes = {
            "/version": lambda: "4.0.0-test",
            "/servers": lambda: self.servers,
            "/servers/srv1/resources": lambda: [{"uuid": "app1"}, {"uuid": "svc1"}, {"uuid": "db1"}],
            "/applications": lambda: self.apps,
            "/services": lambda: self.services,
            "/databases": lambda: self.databases,
            "/deployments": lambda: self.running,
            "/applications/app1/logs": lambda: {"logs": self.app_logs},
            "/deployments/applications/app1": lambda: {"count": len(self.deployments),
                                                        "deployments": list(self.deployments.values())},
        }
        if path in routes:
            return httpx.Response(200, json=routes[path]())
        if path.startswith("/deployments/"):
            uuid = path.split("/")[-1]
            if uuid in self.deployments:
                return httpx.Response(200, json=self.deployments[uuid])
            return httpx.Response(404, json={"message": "Not found"})
        if path.endswith(("/restart", "/start", "/stop")):
            if request.method == "POST":
                return httpx.Response(405, json={"message": "Method not allowed"})
            return httpx.Response(200, json={"message": f"{path.rsplit('/', 1)[1].capitalize()} request queued."})
        if path == "/deploy":
            return httpx.Response(200, json={"deployments": [{"message": "Deployment queued.", "resource_uuid": "app1"}]})
        return httpx.Response(404, json={"message": f"no route {path}"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def fail_deployment(self, uuid: str = "dep1"):
        logs = [
            {"output": "Starting deployment of acme/api", "type": "stdout", "hidden": False},
            {"output": "npm ERR! code ELIFECYCLE", "type": "stderr", "hidden": False},
            {"output": "npm ERR! Missing script: build", "type": "stderr", "hidden": False},
            {"output": "Oops something is not okay, are you okay? 😢", "type": "stderr", "hidden": False},
        ]
        self.deployments[uuid] = {"deployment_uuid": uuid, "application_name": "api", "status": "failed",
                                  "commit": "abc1234def", "created_at": "2026-10-18T10:00:00Z",
                                  "deployment_url": "/project/p/environment/e/application/app1/deployment/" + uuid,
                                  "logs": json.dumps(logs)}
