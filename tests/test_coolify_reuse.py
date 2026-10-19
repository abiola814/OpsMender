"""OpsMender reusing what's already configured in Coolify: SSH keys, notification channels, logs."""
import time

import pytest
from fake_coolify import PEM_OLD, PEM_SERVER

from opsmender import notify
from opsmender.adapters.base import Resource
from opsmender.metrics import autosetup, ssh
from opsmender.monitor.sync import sync


@pytest.fixture
def ssh_calls(monkeypatch):
    calls = []

    async def fake_run_setup(host, port, user, pem, script, timeout=180.0):
        calls.append({"host": host, "port": port, "user": user, "pem": pem, "script": script})
        if pem != PEM_SERVER:
            raise ssh.KeyRejected("rejected")
        return "SHA256:hostkey", "OpsMender probe installed."
    monkeypatch.setattr(ssh, "run_setup", fake_run_setup)
    return calls


async def test_logs_for_databases_and_services(rt):
    client = rt.coolify()
    db_logs = await client.resource_logs(Resource(uuid="db1", kind="database", name="postgres"), 50)
    assert "ready to accept connections" in db_logs
    svc_logs = await client.resource_logs(Resource(uuid="svc1", kind="service", name="plausible"), 50)
    assert "===== plausible =====" in svc_logs and "plausible-db: started" in svc_logs


async def test_auto_setup_uses_the_servers_own_coolify_key_once(rt, fake, ssh_calls):
    await sync(rt)
    server = rt.db.one("SELECT * FROM servers WHERE uuid = 'srv1'")
    assert server["private_key_id"] == 2
    message = await autosetup.setup_server(rt, server)
    assert "not stored" in message
    assert len(ssh_calls) == 1 and ssh_calls[0]["pem"] == PEM_SERVER  # matched by private_key_id, git key skipped
    assert ssh_calls[0]["host"] == "host.docker.internal" and ssh_calls[0]["user"] == "root"
    assert "OPSMENDER_PROBE_EOF" in ssh_calls[0]["script"]
    row = rt.db.one("SELECT * FROM servers WHERE uuid = 'srv1'")
    assert row["metrics_enabled"] == 1 and row["host_key"] == "SHA256:hostkey" and row["auto_setup_error"] is None
    # Coolify's key must not be persisted anywhere.
    dump = "\n".join(str(r) for t in ("settings", "servers", "audit", "events") for r in rt.db.all(f"SELECT * FROM {t}"))
    assert "server\n-----END" not in dump and PEM_SERVER not in dump


async def test_auto_setup_tries_other_keys_when_unmatched(rt, fake, ssh_calls):
    fake.servers[0]["private_key_id"] = None
    fake.keys = [k for k in fake.keys if k["id"] != 2] + [fake.keys[1]]  # wrong key listed first
    await sync(rt)
    await autosetup.setup_server(rt, rt.db.one("SELECT * FROM servers WHERE uuid = 'srv1'"))
    assert [c["pem"] for c in ssh_calls] == [PEM_OLD, PEM_SERVER]


async def test_auto_setup_explains_missing_permission(rt, fake, ssh_calls):
    fake.sensitive = False
    await sync(rt)
    with pytest.raises(autosetup.SetupError, match="read:sensitive"):
        await autosetup.setup_server(rt, rt.db.one("SELECT * FROM servers WHERE uuid = 'srv1'"))
    assert ssh_calls == []


def test_needs_setup_rules():
    now = time.time()
    assert autosetup.needs_setup({"metrics_enabled": 0}, None)
    assert not autosetup.needs_setup({"metrics_enabled": 0, "reachable": 0}, None)
    assert not autosetup.needs_setup({"metrics_enabled": 0, "auto_setup_error": autosetup.DISABLED_BY_USER}, None)
    assert not autosetup.needs_setup({"metrics_enabled": 0, "auto_setup_error": "x", "auto_setup_at": now}, None)
    assert autosetup.needs_setup({"metrics_enabled": 0, "auto_setup_error": "x", "auto_setup_at": now - 7 * 3600}, None)
    assert autosetup.needs_setup({"metrics_enabled": 1}, 1)       # outdated probe gets updated
    assert not autosetup.needs_setup({"metrics_enabled": 1}, 2)


async def test_setup_pending_runs_after_sync_when_enabled(rt, fake, ssh_calls, monkeypatch):
    async def no_poll(*a):
        return None
    await sync(rt)
    await autosetup.setup_pending(rt)
    assert len(ssh_calls) == 1
    rt.db.execute("UPDATE servers SET metrics_enabled = 0, auto_setup_error = ?", (autosetup.DISABLED_BY_USER,))
    await autosetup.setup_pending(rt)
    assert len(ssh_calls) == 1  # a user's "disable" is respected


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return True, "sent"
    monkeypatch.setattr(notify, "_post", fake_post)
    return calls


async def test_reads_enabled_coolify_channels(rt):
    channels = {c.name: c for c in await notify.coolify_channels(rt)}
    assert set(channels) == {"discord", "telegram", "email"}  # slack/pushover/webhook disabled
    assert channels["discord"].ready and channels["telegram"].ready
    assert not channels["email"].ready and "root-team" in channels["email"].problem


async def test_alerts_go_to_coolify_channels_without_duplicates(rt, sent):
    results = await notify.dispatch(rt, notify.Alert(title="api is crash-looping", kind="crash_loop",
                                                     severity="critical", url="https://ops.test/incidents/1"))
    urls = [u for u, _ in sent]
    assert "https://discord.test/hook" in urls
    assert "https://api.telegram.org/bot123:abc/sendMessage" in urls
    assert any("crash-looping" in kw["json"]["content"] for u, kw in sent if "discord" in u)

    sent.clear()
    results = await notify.dispatch(rt, notify.Alert(title="Deployment of api failed", kind="deployment_failed"))
    skipped = {d: detail for d, ok, detail in results if ok is None}
    assert skipped["Coolify Discord"] == "Coolify already sends this alert"  # Discord has deployment_failure on
    assert [u for u, _ in sent] == ["https://api.telegram.org/bot123:abc/sendMessage"]


async def test_channels_without_secrets_are_reported_not_used(rt, fake, sent):
    fake.sensitive = False
    channels = await notify.coolify_channels(rt, refresh=True)
    assert all(not c.ready for c in channels)
    assert "read:sensitive" in channels[0].problem
    await notify.dispatch(rt, notify.Alert(title="x", kind="crash_loop"))
    assert sent == []


async def test_old_coolify_without_notification_api(rt, fake, sent):
    fake.notifications_supported = False
    assert await notify.coolify_channels(rt, refresh=True) == []
    rt.settings.set("notify_webhook_url", "https://hooks.example/abc")
    results = await notify.dispatch(rt, notify.Alert(title="x", kind="crash_loop"))
    assert results == [("OpsMender webhook", True, "sent")]


async def test_check_access_reports_capabilities(rt, fake):
    from opsmender.web.routes.settings import check_access
    access = {a["key"]: a for a in await check_access(rt.coolify())}
    assert access["inventory"]["ok"] and access["ssh_keys"]["ok"] and access["notifications"]["ok"]
    fake.sensitive = False
    fake.notifications_supported = False
    access = {a["key"]: a for a in await check_access(rt.coolify())}
    assert not access["ssh_keys"]["ok"] and "read:sensitive" in access["ssh_keys"]["detail"]
    assert not access["notifications"]["ok"] and "version" in access["notifications"]["detail"]
