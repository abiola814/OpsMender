import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from opsmender.adapters.coolify import CoolifyClient
from opsmender.monitor.sync import sync
from opsmender.web.app import create_app
from opsmender.web.common import markdown

from conftest import make_config


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@pytest.fixture
def client(tmp_path, fake):
    app = create_app(make_config(tmp_path), start_background=False)
    with TestClient(app) as c:
        rt = app.state.rt
        api = CoolifyClient("https://coolify.test", fake.token, transport=fake.transport())
        rt.coolify = lambda: api
        rt.settings.set("coolify_url", "https://coolify.test")
        rt.settings.set("coolify_token", fake.token)
        c.rt = rt
        yield c


def setup_admin(c):
    page = c.get("/setup")
    code = c.app.state.setup_code
    r = c.post("/setup", data={"csrf": csrf_of(page.text), "setup_code": code, "username": "admin",
                               "password": "correct-horse-1", "password_confirm": "correct-horse-1"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings"


def test_first_run_requires_setup_code(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/setup"
    page = client.get("/setup")
    r = client.post("/setup", data={"csrf": csrf_of(page.text), "setup_code": "WRONG", "username": "admin",
                                    "password": "correct-horse-1", "password_confirm": "correct-horse-1"})
    assert "setup code doesn" in r.text
    assert client.rt.db.scalar("SELECT COUNT(*) FROM users") == 0


def test_csrf_is_enforced(client):
    setup_admin(client)
    r = client.post("/sync", data={"csrf": "forged"})
    assert r.status_code == 403


def test_pages_render_with_synced_data(client, fake):
    setup_admin(client)
    asyncio.run(sync(client.rt))
    home = client.get("/")
    assert home.status_code == 200
    assert "main" in home.text  # server card
    assert re.search(r'<div class="label">Resources</div>\s*<div class="value">3</div>', home.text)
    assert "All resources look healthy" in home.text
    for url in ("/servers", "/servers/srv1", "/resources", "/resources/app1", "/resources/db1", "/incidents",
                "/incidents?status=all", "/activity", "/settings"):
        assert client.get(url).status_code == 200, url
    logs = client.get("/resources/app1/logs").json()
    assert "Server listening" in logs["logs"]
    assert client.get("/resources/nope").status_code == 404


def test_incident_page_shows_rule_findings(client, fake):
    setup_admin(client)
    asyncio.run(sync(client.rt))
    fake.running = [{"deployment_uuid": "dep1", "application_name": "api", "status": "in_progress"}]
    asyncio.run(sync(client.rt))
    fake.running = []
    fake.fail_deployment("dep1")

    async def run():
        from opsmender import incidents
        await sync(client.rt)
        while incidents._tasks:
            await asyncio.gather(*list(incidents._tasks), return_exceptions=True)
    asyncio.run(run())
    inc_id = client.rt.db.scalar("SELECT id FROM incidents")
    page = client.get(f"/incidents/{inc_id}")
    assert "Deployment of api failed" in page.text
    assert "Build step failed" in page.text
    assert "Connect an AI provider" in page.text


def test_actions_need_opt_in_and_typed_confirmation(client, fake):
    setup_admin(client)
    asyncio.run(sync(client.rt))
    page = client.get("/resources/app1")
    token = csrf_of(page.text)
    r = client.post("/resources/app1/action", data={"csrf": token, "action": "restart", "confirm": "api"})
    assert "Actions are turned off" in r.text
    client.rt.settings.set("actions_enabled", True)
    r = client.post("/resources/app1/action", data={"csrf": token, "action": "restart", "confirm": "wrong"})
    assert "to confirm" in r.text
    r = client.post("/resources/app1/action", data={"csrf": token, "action": "restart", "confirm": "api"})
    assert "Restart requested" in r.text
    assert client.rt.db.one("SELECT action, result FROM audit WHERE action = 'restart'") == \
        {"action": "restart", "result": "ok"}


def test_server_disk_analysis_page(client, monkeypatch):
    from opsmender.metrics import ssh
    from test_disk import RAW

    async def fake_disk(*args):
        return RAW
    monkeypatch.setattr(ssh, "disk", fake_disk)
    setup_admin(client)
    asyncio.run(sync(client.rt))
    client.rt.settings.set("ssh_private_key", __import__("opsmender.metrics.ssh", fromlist=["x"]).generate_keypair()[0])
    from opsmender.db import dumps
    from opsmender.metrics.compute import compute
    from test_metrics import snap
    current = compute(snap(1000, 10_000, 8_000, usec=0), {**snap(1060, 11_000, 8_250, usec=30_000_000), "v": 2})
    client.rt.db.execute("UPDATE servers SET metrics_enabled = 1, current_metrics = ? WHERE uuid = 'srv1'",
                         (dumps(current),))
    page = client.get("/servers/srv1")
    assert "Analyze disk usage" in page.text
    r = client.post("/servers/srv1/disk", data={"csrf": csrf_of(page.text)})
    assert "Nothing was deleted" in r.text
    assert "Build cache" in r.text and "Unused volumes" in r.text and "danger" in r.text
    assert "docker builder prune" in r.text


def test_webhook_requires_secret_token(client):
    setup_admin(client)
    client.get("/settings")
    token = client.rt.settings.get("webhook_token")
    assert client.post("/webhooks/coolify/wrong", json={}).status_code == 404
    r = client.post(f"/webhooks/coolify/{token}", json={"event": "deployment_failed", "message": "api failed"})
    assert r.json() == {"ok": True}
    assert client.rt.wake.is_set()
    assert "deployment_failed" in client.rt.db.one("SELECT message FROM events WHERE kind = 'coolify_webhook'")["message"]


def test_secrets_are_encrypted_at_rest(client):
    raw = client.rt.db.one("SELECT value FROM settings WHERE key = 'coolify_token'")["value"]
    assert "test-token" not in raw
    assert client.rt.settings.get("coolify_token") == "test-token"


def test_markdown_is_escaped():
    html = str(markdown("## Fix\n1. Set `PORT`\n<script>alert(1)</script>\n```\nrm -rf /\n```"))
    assert "<h4>Fix</h4>" in html and "<code>PORT</code>" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
