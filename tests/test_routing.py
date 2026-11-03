"""Per-resource notification routing: only some destinations, a minimum severity, mute, an extra webhook."""
import asyncio

import pytest

from opsmender import notify
from opsmender.incidents import open_incident, resolve_incident
from opsmender.monitor.sync import sync

DISCORD = "https://discord.test/hook"
TELEGRAM = "https://api.telegram.org/bot123:abc/sendMessage"


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_post(url, **kwargs):
        calls.append(url)
        return True, "sent"
    monkeypatch.setattr(notify, "_post", fake_post)
    return calls


def alert(**kw):
    return notify.Alert(title="api is crash-looping", kind="crash_loop", severity="critical", **kw)


async def test_without_rules_alerts_go_everywhere(rt, sent):
    await notify.dispatch(rt, alert(resource_uuid="app1"))
    assert sorted(sent) == sorted([DISCORD, TELEGRAM])


async def test_resource_rule_picks_destinations_and_extra_webhook(rt, sent):
    notify.save_route(rt, notify.Route("resource", "app1", "only", "info", ("coolify:telegram",),
                                       "https://hooks.team-api.test/x"))
    results = await notify.dispatch(rt, alert(resource_uuid="app1"))
    assert sent == [TELEGRAM, "https://hooks.team-api.test/x"]
    assert ("This resource's webhook", True, "sent") in results
    sent.clear()
    await notify.dispatch(rt, alert(resource_uuid="db1"))   # other resources are unaffected
    assert sorted(sent) == sorted([DISCORD, TELEGRAM])
    stored = rt.db.one("SELECT extra_webhook FROM notification_routes")["extra_webhook"]
    assert "team-api" not in stored                         # encrypted at rest


async def test_minimum_severity_and_resolved_alerts(rt, sent):
    notify.save_route(rt, notify.Route("resource", "app1", "default", "critical"))
    results = await notify.dispatch(rt, notify.Alert(title="High CPU", kind="high_cpu", severity="warning",
                                                     resource_uuid="app1"))
    assert sent == [] and "minimum severity" in results[0][2]
    # A "resolved" alert is routed by the severity of the incident it resolves.
    await notify.dispatch(rt, notify.Alert(title="Resolved", severity="resolved", kind="x_resolved",
                                           resource_uuid="app1", level="warning"))
    assert sent == []
    await notify.dispatch(rt, notify.Alert(title="Resolved", severity="resolved", kind="x_resolved",
                                           resource_uuid="app1", level="critical"))
    assert len(sent) == 2


async def test_server_rule_covers_its_resources_unless_they_have_their_own(rt, sent):
    await sync(rt)
    notify.save_route(rt, notify.Route("server", "srv1", "mute"))
    results = await notify.dispatch(rt, alert(resource_uuid="app1"))
    assert sent == [] and "muted" in results[0][2]
    notify.save_route(rt, notify.Route("resource", "app1", "only", "info", ("coolify:discord",)))
    await notify.dispatch(rt, alert(resource_uuid="app1"))
    assert sent == [DISCORD]
    sent.clear()
    await notify.dispatch(rt, alert(resource_uuid="db1"))   # still muted through the server rule
    assert sent == []


async def test_default_rule_is_the_same_as_no_rule(rt):
    notify.save_route(rt, notify.Route("resource", "app1", "mute"))
    notify.save_route(rt, notify.Route("resource", "app1", "default", "info"))
    assert rt.db.scalar("SELECT COUNT(*) FROM notification_routes") == 0


async def test_incidents_carry_their_resource_into_routing(rt, sent):
    notify.save_route(rt, notify.Route("resource", "app1", "mute"))
    resource = {"uuid": "app1", "kind": "application", "name": "api", "server_uuid": None}
    open_incident(rt, fingerprint="x", kind="crash_loop", severity="critical", title="api down", resource=resource,
                  enrich_now=False)
    resolve_incident(rt, "x")
    from opsmender import incidents
    while incidents._tasks:
        await asyncio.gather(*list(incidents._tasks), return_exceptions=True)
    assert sent == []
