"""In-memory stand-in for the Coolify API, served through httpx.MockTransport."""

from __future__ import annotations

import json

import httpx

PEM_GIT = "-----BEGIN OPENSSH PRIVATE KEY-----\ngit\n-----END OPENSSH PRIVATE KEY-----"
PEM_SERVER = "-----BEGIN OPENSSH PRIVATE KEY-----\nserver\n-----END OPENSSH PRIVATE KEY-----"
PEM_OLD = "-----BEGIN OPENSSH PRIVATE KEY-----\nold\n-----END OPENSSH PRIVATE KEY-----"


class FakeCoolify:
    def __init__(self):
        self.token = "test-token"
        self.servers = [{"uuid": "srv1", "name": "main", "ip": "host.docker.internal", "port": 22, "user": "root",
                         "settings": {"is_reachable": True, "is_usable": True}, "private_key_id": 2}]
        self.apps = [{"uuid": "app1", "name": "api", "status": "running:healthy", "fqdn": "https://api.example.com",
                      "git_repository": "acme/api", "git_branch": "main", "build_pack": "nixpacks",
                      "environment_variables": [{"key": "SECRET", "value": "x"}]}]
        self.services = [{"uuid": "svc1", "name": "plausible", "status": "running:healthy"}]
        self.databases = [{"uuid": "db1", "name": "postgres", "status": "running:healthy",
                           "postgres_password": "hunter2"}]
        self.running: list[dict] = []
        self.deployments: dict[str, dict] = {}
        self.app_logs = "Server listening on 0.0.0.0:3000"
        self.sensitive = True  # whether the token has read:sensitive
        self.keys = [
            {"id": 1, "uuid": "k1", "name": "github-app", "is_git_related": True, "private_key": PEM_GIT},
            {"id": 2, "uuid": "k2", "name": "localhost's key", "is_git_related": False, "private_key": PEM_SERVER},
            {"id": 3, "uuid": "k3", "name": "old key", "is_git_related": False, "private_key": PEM_OLD},
        ]
        self.notifications = {
            "discord": {"discord_enabled": True, "discord_webhook_url": "https://discord.test/hook",
                        "deployment_failure_discord_notifications": True, "status_change_discord_notifications": False},
            "slack": {"slack_enabled": False, "slack_webhook_url": None},
            "telegram": {"telegram_enabled": True, "telegram_token": "123:abc", "telegram_chat_id": "42"},
            "pushover": {"pushover_enabled": False},
            "webhook": {"webhook_enabled": False},
            "email": {"smtp_enabled": False, "resend_enabled": False, "use_instance_email_settings": True,
                      "smtp_recipients": "ops@example.com"},
        }
        self.notifications_supported = True
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
        if path == "/security/keys":
            keys = [k if self.sensitive else {x: v for x, v in k.items() if x != "private_key"} for k in self.keys]
            return httpx.Response(200, json=keys)
        if path.startswith("/notifications/"):
            if not self.notifications_supported:
                return httpx.Response(404, json={"message": "Not found"})
            data = dict(self.notifications[path.rsplit("/", 1)[1]])
            if not self.sensitive:
                data = {k: (None if any(w in k for w in ("url", "token", "key", "password")) else v)
                        for k, v in data.items()}
            return httpx.Response(200, json=data)
        if path == "/settings/email":
            return httpx.Response(403, json={"message": "Root team only."})
        if path == "/databases/db1/logs":
            return httpx.Response(200, json={"logs": "LOG:  database system is ready to accept connections"})
        if path == "/services/svc1":
            return httpx.Response(200, json={"uuid": "svc1", "applications": [{"name": "plausible"}],
                                             "databases": [{"name": "plausible-db"}]})
        if path == "/services/svc1/logs":
            name = request.url.params.get("sub_service_name")
            return httpx.Response(200, json={"logs": f"{name}: started"})
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
