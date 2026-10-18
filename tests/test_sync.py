import asyncio

from opsmender import incidents
from opsmender.adapters.base import health_of
from opsmender.db import loads
from opsmender.monitor.sync import sync


async def drain():
    while incidents._tasks:
        await asyncio.gather(*list(incidents._tasks), return_exceptions=True)


def test_health_of_maps_coolify_statuses():
    assert health_of("running:healthy") == "ok"
    assert health_of("running:unknown") == "ok"
    assert health_of("running:unhealthy") == "warning"
    assert health_of("degraded:unhealthy") == "warning"
    assert health_of("restarting:unknown") == "down"
    assert health_of("exited:unhealthy") == "stopped"
    assert health_of(None) == "unknown"


async def test_sync_discovers_inventory_without_secrets(rt):
    await sync(rt)
    names = {r["name"]: r for r in rt.db.all("SELECT * FROM resources")}
    assert set(names) == {"api", "plausible", "postgres"}
    assert names["api"]["server_uuid"] == "srv1"
    assert "hunter2" not in names["postgres"]["raw"]
    assert "environment_variables" not in names["api"]["raw"]
    assert rt.db.one("SELECT name FROM servers")["name"] == "main"
    assert rt.status["coolify_version"] == "4.0.0-test"
    # First sync never raises incidents for pre-existing state.
    assert rt.db.scalar("SELECT COUNT(*) FROM incidents") == 0


async def test_crash_opens_and_recovery_resolves_incident(rt, fake):
    await sync(rt)
    fake.apps[0]["status"] = "restarting:unknown"
    await sync(rt)
    await drain()
    inc = rt.db.one("SELECT * FROM incidents WHERE fingerprint = 'down:app1'")
    assert inc["status"] == "open" and inc["title"] == "api is restarting"
    evidence = loads(inc["evidence"])
    assert "Server listening" in evidence["logs"]  # app logs fetched on demand via the Coolify API
    assert evidence["deployments"] == []  # evidence collection ran to completion
    assert loads(inc["findings"]) is not None

    await sync(rt)  # still down: same incident, not a duplicate
    assert rt.db.scalar("SELECT COUNT(*) FROM incidents") == 1

    fake.apps[0]["status"] = "running:healthy"
    await sync(rt)
    assert rt.db.one("SELECT status FROM incidents WHERE id = ?", (inc["id"],))["status"] == "resolved"


async def test_user_stop_is_not_an_incident(rt, fake):
    await sync(rt)
    rt.suppressed["app1"] = 9e18
    fake.apps[0]["status"] = "exited:unhealthy"
    await sync(rt)
    assert rt.db.scalar("SELECT COUNT(*) FROM incidents") == 0


async def test_failed_deployment_is_diagnosed_from_build_log(rt, fake):
    await sync(rt)
    fake.running = [{"deployment_uuid": "dep1", "application_name": "api", "status": "in_progress",
                     "deployment_url": "/project/p/environment/e/application/app1/deployment/dep1"}]
    await sync(rt)
    assert rt.db.one("SELECT * FROM deployments WHERE uuid = 'dep1'")["app_uuid"] == "app1"

    fake.running = []
    fake.fail_deployment("dep1")
    await sync(rt)
    await drain()
    inc = rt.db.one("SELECT * FROM incidents WHERE kind = 'deployment_failed'")
    assert inc["title"] == "Deployment of api failed"
    findings = loads(inc["findings"])
    assert findings["findings"][0]["rule_id"] == "build_failed"
    assert "npm ERR! Missing script: build" in loads(inc["evidence"])["logs"]
    assert rt.db.one("SELECT final, status FROM deployments WHERE uuid = 'dep1'") == {"final": 1, "status": "failed"}


async def test_actions_fall_back_to_get_and_redeploy(rt, fake):
    client = rt.coolify()
    assert await client.perform("application", "app1", "restart") == "Restart request queued."
    assert ("POST", "/applications/app1/restart") in fake.calls and ("GET", "/applications/app1/restart") in fake.calls
    assert await client.perform("application", "app1", "redeploy") == "Deployment queued."


async def test_bad_token_reports_clearly(rt, fake):
    fake.token = "rotated"
    try:
        await sync(rt)
    except Exception as exc:  # noqa: BLE001
        assert "401" in str(exc)
    assert "401" in rt.status["last_sync_error"]
