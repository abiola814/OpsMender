import json

import anthropic
import httpx
import httpx2
import pytest

from opsmender import incidents
from opsmender.ai import providers
from opsmender.db import loads
from opsmender.investigation import TOOLS, Toolbox
from opsmender.monitor.sync import sync


@pytest.fixture
async def incident(rt, fake):
    await sync(rt)
    fake.fail_deployment("dep1")
    rt.db.execute("INSERT INTO deployments (uuid, app_uuid, app_name, status, commit_sha, created_at, final)"
                  " VALUES ('dep1', 'app1', 'api', 'failed', 'abc1234def', '2026-10-18T10:00:00Z', 1)")
    inc_id, _ = incidents.open_incident(rt, fingerprint="deploy:dep1", kind="deployment_failed", severity="critical",
                                        title="Deployment of api failed", resource={"uuid": "app1", "name": "api",
                                                                                    "kind": "application"},
                                        enrich_now=False)
    return rt.db.one("SELECT * FROM incidents WHERE id = ?", (inc_id,))


def use_openai(rt, monkeypatch, handler):
    rt.settings.set("ai_provider", "openai")
    rt.settings.set("ai_model", "test-model")
    rt.settings.set("ai_api_key", "sk-test")
    requests = []
    real = httpx.AsyncClient

    def recording(request):
        requests.append(json.loads(request.content))
        return handler(request, len(requests))

    monkeypatch.setattr(providers.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(recording), **kw))
    return requests


def chat(message):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", **message}}]})


def tool_call(id_, name, args):
    return {"id": id_, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


async def test_tools_read_what_opsmender_can_see(rt, incident):
    box = Toolbox(rt, incident)
    out, err = await box.run("get_resource", {})
    assert not err and json.loads(out)["config"]["build_pack"] == "nixpacks"

    out, err = await box.run("get_env_var_names", {"resource": "api"})
    assert not err and json.loads(out)["names"] == ["DATABASE_URL", "PORT"]
    assert "hunter2" not in out

    out, err = await box.run("get_deployments", {})
    assert not err and json.loads(out)["deployments"][0]["uuid"] == "dep1"
    out, err = await box.run("get_deployment_log", {"deployment_uuid": "dep1"})
    assert not err and "npm ERR! Missing script: build" in out and "status failed" in out

    out, err = await box.run("get_logs", {"lines": 50})
    assert not err and "Server listening" in out

    out, err = await box.run("list_resources", {})
    assert not err and {r["name"] for r in json.loads(out)["resources"]} == {"api", "plausible", "postgres"}


async def test_tool_errors_go_back_to_the_model(rt, incident):
    box = Toolbox(rt, incident)
    assert (await box.run("get_logs", {"resource": "nope"}))[1]
    assert (await box.run("get_deployment_log", {"deployment_uuid": "../../etc"}))[1]
    assert (await box.run("get_env_var_names", {"resource": "postgres"}))[1]
    assert (await box.run("rm_rf", {}))[1]
    out, err = await box.run("get_server_metrics", {})
    assert err and "Metrics aren't set up" in out


async def test_openai_investigation_runs_tools_and_records_steps(rt, monkeypatch, incident):
    def handler(request, n):
        if n == 1:
            return chat({"content": None, "tool_calls": [tool_call("c1", "get_env_var_names", {}),
                                                         tool_call("c2", "get_deployments", {})]})
        return chat({"content": "## What happened\nThe build script is missing."})

    requests = use_openai(rt, monkeypatch, handler)
    answer = await incidents.diagnose(rt, incident["id"])
    assert answer.startswith("## What happened")
    assert {t["function"]["name"] for t in requests[0]["tools"]} == {t.name for t in TOOLS}
    tool_messages = [m for m in requests[1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["c1", "c2"]
    assert "DATABASE_URL" in tool_messages[0]["content"]
    steps = loads(rt.db.one("SELECT ai_steps FROM incidents WHERE id = ?", (incident["id"],))["ai_steps"])
    assert [s["tool"] for s in steps] == ["get_env_var_names", "get_deployments"] and all(s["ok"] for s in steps)


async def test_tool_budget_is_enforced(rt, monkeypatch, incident):
    rt.settings.set("ai_max_tool_calls", 1)

    def handler(request, n):
        if n == 1:
            return chat({"content": "", "tool_calls": [tool_call("c1", "get_resource", {}),
                                                       tool_call("c2", "get_logs", {})]})
        return chat({"content": "done"})

    requests = use_openai(rt, monkeypatch, handler)
    await incidents.diagnose(rt, incident["id"])
    assert requests[1]["tool_choice"] == "none"
    skipped = [m for m in requests[1]["messages"] if m.get("tool_call_id") == "c2"][0]
    assert skipped["content"] == providers.BUDGET_SPENT


async def test_models_without_tools_fall_back_to_one_request(rt, monkeypatch, incident):
    def handler(request, n):
        if "tools" in json.loads(request.content):
            return httpx.Response(400, json={"error": "llama2 does not support tools"})
        return chat({"content": "single-shot answer"})

    requests = use_openai(rt, monkeypatch, handler)
    assert await incidents.diagnose(rt, incident["id"]) == "single-shot answer"
    assert len(requests) == 2


async def test_investigation_can_be_turned_off(rt, monkeypatch, incident):
    rt.settings.set("ai_investigate", False)
    requests = use_openai(rt, monkeypatch, lambda request, n: chat({"content": "plain"}))
    assert await incidents.diagnose(rt, incident["id"]) == "plain"
    assert "tools" not in requests[0]


async def test_claude_investigation_round_trips_tool_use(rt, monkeypatch, incident):
    rt.settings.set("ai_provider", "anthropic")
    rt.settings.set("ai_model", "claude-opus-5")
    rt.settings.set("ai_api_key", "sk-ant-test")
    bodies = []

    def handler(request):
        bodies.append((request.headers.get("anthropic-beta"), json.loads(request.content)))
        base = {"id": f"msg{len(bodies)}", "type": "message", "role": "assistant", "model": "claude-opus-5",
                "usage": {"input_tokens": 10, "output_tokens": 10}}
        if len(bodies) == 1:
            return httpx2.Response(200, json={**base, "stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "tu1", "name": "get_deployment_log", "input": {"deployment_uuid": "dep1"}}]})
        return httpx2.Response(200, json={**base, "stop_reason": "end_turn",
                                          "content": [{"type": "text", "text": "Missing build script."}]})

    monkeypatch.setattr(providers, "_anthropic_client", lambda cfg: anthropic.AsyncAnthropic(
        api_key=cfg.api_key, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))))
    assert await incidents.diagnose(rt, incident["id"]) == "Missing build script."
    beta, first = bodies[0]
    assert "server-side-fallback-2026-07-01" in beta and first["fallbacks"] == "default"
    assert first["tool_choice"] == {"type": "auto"} and first["cache_control"] == {"type": "ephemeral"}
    result = bodies[1][1]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "tu1"
    assert "Missing script: build" in result["content"]
