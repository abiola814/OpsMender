"""Fix pull requests: reading the repo, checking proposed edits, and opening a PR only after review."""
import asyncio
import base64
import json

import httpx
import pytest

from opsmender import fixes
from opsmender.ai.providers import Investigation
from opsmender.github import GitHubClient, GitHubError, parse_repo
from opsmender.monitor.sync import sync

SERVER_JS = "const express = require('express');\nconst app = express();\napp.listen(3000);\n"


class FakeGitHub:
    def __init__(self):
        self.files = {"src/server.js": SERVER_JS, "package.json": '{"name": "api"}\n', "README.md": "# api\n",
                      "node_modules/x/index.js": "x", ".github/workflows/ci.yml": "on: push\n"}
        self.head = "sha-main"
        self.posts: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.headers.get("authorization") != "Bearer gh-token":
            return httpx.Response(401, json={"message": "Bad credentials"})
        if request.method == "POST":
            body = json.loads(request.content)
            self.posts.append((path, body))
            if path.endswith("/pulls"):
                return httpx.Response(201, json={"html_url": "https://github.com/acme/api/pull/7", "number": 7})
            return httpx.Response(201, json={"sha": f"new-{len(self.posts)}"})
        if path == "/user":
            return httpx.Response(200, json={"login": "octo"})
        if path == "/repos/acme/api":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == "/repos/acme/api/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": self.head}})
        if path.startswith("/repos/acme/api/git/trees/"):
            return httpx.Response(200, json={"tree": [{"path": p, "type": "blob", "size": len(c), "mode": "100644"}
                                                      for p, c in self.files.items()]})
        if path.startswith("/repos/acme/api/git/commits/"):
            return httpx.Response(200, json={"tree": {"sha": "tree-1"}})
        if path.startswith("/repos/acme/api/contents/"):
            name = path.removeprefix("/repos/acme/api/contents/")
            if name not in self.files:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"type": "file", "encoding": "base64",
                                             "content": base64.b64encode(self.files[name].encode()).decode()})
        return httpx.Response(404, json={"message": f"no route {path}"})


@pytest.fixture
def gh(rt, monkeypatch):
    fake = FakeGitHub()
    rt.settings.set("github_token", "gh-token")
    rt.settings.set("ai_provider", "anthropic")
    rt.settings.set("ai_api_key", "k")
    monkeypatch.setattr(fixes, "github_client",
                        lambda rt: GitHubClient("gh-token", transport=httpx.MockTransport(fake.handler)))
    return fake


def scripted_ai(monkeypatch, calls):
    """Replace the model with a fixed sequence of tool calls; records each result."""
    results = []

    async def fake_investigate(cfg, prompt, tools, run_tool, max_calls=8, system=None, max_tokens=4000):
        assert "propose_fix" in [t.name for t in tools] and "get_logs" in [t.name for t in tools]
        assert "acme/api, branch main" in prompt
        for name, args in calls:
            results.append(await run_tool(name, args))
        return Investigation("The app ignores PORT, so Coolify's proxy can't reach it.", [])
    monkeypatch.setattr(fixes, "investigate", fake_investigate)
    return results


async def incident(rt):
    await sync(rt)
    rt.db.execute("INSERT INTO incidents (id, fingerprint, kind, severity, title, resource_kind, resource_uuid,"
                  " resource_name, first_seen, last_seen, evidence, findings) VALUES (1, 'x', 'resource_down',"
                  " 'critical', 'api is down', 'application', 'app1', 'api', 1, 1, '{}', '{}')")
    return 1


GOOD_EDIT = {"path": "src/server.js", "old_text": "app.listen(3000);",
             "new_text": "app.listen(process.env.PORT || 3000, '0.0.0.0');"}
PROPOSAL = {"title": "Listen on the PORT Coolify provides",
            "explanation": "## Root cause\nHard-coded port.\n## Change\nUse PORT.\n## How to verify\nDeploy.",
            "edits": [GOOD_EDIT]}


async def test_draft_checks_edits_then_opens_a_pr_on_a_new_branch(rt, gh, monkeypatch):
    inc_id = await incident(rt)
    results = scripted_ai(monkeypatch, [
        ("list_files", {}),
        ("propose_fix", PROPOSAL),                                        # not read yet
        ("read_file", {"path": "src/server.js"}),
        ("propose_fix", {**PROPOSAL, "edits": [{**GOOD_EDIT, "old_text": "app"}]}),   # ambiguous match
        ("propose_fix", {**PROPOSAL, "edits": [GOOD_EDIT, {"path": ".github/workflows/ci.yml", "old_text": "",
                                                           "new_text": "x"}]}),       # CI is off limits
        ("propose_fix", PROPOSAL),
    ])
    draft = await fixes.draft_fix(rt, inc_id, "admin")
    listing, not_read, ambiguous, ci = (results[i][0] for i in (0, 1, 3, 4))
    assert "src/server.js" in listing and "node_modules" not in listing
    assert "read src/server.js before editing it" in not_read
    assert "appears 2 times" in ambiguous
    assert "CI workflows" in ci
    assert results[5] == ("Fix recorded for review. Now reply with your short summary.", False)
    assert draft["files"][0]["content"] == SERVER_JS.replace("app.listen(3000);", GOOD_EDIT["new_text"])
    assert gh.posts == []                                                 # nothing written while drafting

    url = await fixes.open_pull_request(rt, inc_id, "admin")
    assert url == "https://github.com/acme/api/pull/7"
    paths = [p for p, _ in gh.posts]
    assert paths == ["/repos/acme/api/git/blobs", "/repos/acme/api/git/trees", "/repos/acme/api/git/commits",
                     "/repos/acme/api/git/refs", "/repos/acme/api/pulls"]
    ref, pr = gh.posts[3][1], gh.posts[4][1]
    assert ref["ref"].startswith("refs/heads/opsmender/incident-1-")
    assert pr["base"] == "main" and pr["draft"] is True and "OpsMender never merges" in pr["body"]
    assert rt.db.one("SELECT fix_pr_url FROM incidents WHERE id = 1")["fix_pr_url"] == url
    assert await fixes.open_pull_request(rt, inc_id, "admin") == url    # idempotent
    assert len(gh.posts) == 5


async def test_pr_refused_when_the_file_changed_since_drafting(rt, gh, monkeypatch):
    inc_id = await incident(rt)
    scripted_ai(monkeypatch, [("read_file", {"path": "src/server.js"}), ("propose_fix", PROPOSAL)])
    await fixes.draft_fix(rt, inc_id)
    gh.head = "sha-newer"
    gh.files["README.md"] = "# api v2\n"                                   # unrelated change: fine
    gh.files["src/server.js"] = SERVER_JS + "// changed upstream\n"       # our file changed: refuse
    with pytest.raises(GitHubError, match="changed on main since the fix was drafted"):
        await fixes.open_pull_request(rt, inc_id)
    assert gh.posts == []


async def test_no_code_fix_is_recorded_too(rt, gh, monkeypatch):
    inc_id = await incident(rt)
    scripted_ai(monkeypatch, [("get_env_var_names", {})])
    draft = await fixes.draft_fix(rt, inc_id)
    assert draft["files"] == [] and "PORT" in draft["summary"]
    with pytest.raises(GitHubError, match="no drafted fix"):
        await fixes.open_pull_request(rt, inc_id)


async def test_availability_explains_what_is_missing(rt, gh):
    await incident(rt)
    inc = rt.db.one("SELECT * FROM incidents WHERE id = 1")
    resource = {"kind": "application", "name": "api", "git_repository": "https://gitlab.com/acme/api"}
    assert "isn't a GitHub repository" in fixes.availability(rt, inc, resource)
    assert "Git repository" in fixes.availability(rt, inc, {"kind": "database", "name": "pg"})
    rt.settings.delete("github_token")
    assert "Connect GitHub" in fixes.availability(rt, inc, {**resource, "git_repository": "acme/api"})


def test_parse_repo_accepts_what_coolify_stores():
    assert parse_repo("acme/api") == ("acme", "api")
    assert parse_repo("https://github.com/acme/api.git") == ("acme", "api")
    assert parse_repo("git@github.com:acme/api.git") == ("acme", "api")
    assert parse_repo("https://x-access-token:abc@github.com/acme/api") == ("acme", "api")
    assert parse_repo("https://gitlab.com/acme/api") is None
    assert parse_repo("https://ghe.corp.test/acme/api", "https://ghe.corp.test/api/v3") == ("acme", "api")
    assert parse_repo("") is None and parse_repo("not a repo") is None


def test_check_file_catches_syntax_and_literal_secrets():
    assert fixes.check_file("a.py", "x = 1\n", "x = (\n") == ["Python syntax error on line 1: '(' was never closed"]
    assert "invalid JSON" in fixes.check_file("a.json", "{}", "{,}")[0]
    assert "secret" in fixes.check_file("a.js", "", 'const db = "postgres://u:hunter2secret@db/app"\n')[0]
    assert fixes.check_file("a.py", "x = 1\n", "x = 1\npassword = os.environ['DB_PASSWORD']\n") == []
    assert fixes.check_file("a.py", "x = 1\n", "x = 1\n") == ["the edits don't change anything"]


def test_fix_card_and_settings_render(rt, gh, monkeypatch, tmp_path, fake):
    from fastapi.testclient import TestClient

    from conftest import make_config
    from opsmender.adapters.coolify import CoolifyClient
    from opsmender.web.app import create_app
    from test_web import csrf_of, setup_admin

    (tmp_path / "web").mkdir()
    app = create_app(make_config(tmp_path / "web"), start_background=False)
    with TestClient(app) as c:
        wrt = app.state.rt
        api = CoolifyClient("https://coolify.test", fake.token, transport=fake.transport())
        wrt.coolify = lambda: api
        for k, v in (("coolify_url", "https://coolify.test"), ("coolify_token", fake.token), ("github_token", "gh-token"),
                     ("ai_provider", "anthropic"), ("ai_api_key", "k")):
            wrt.settings.set(k, v)
        setup_admin(c)
        asyncio.run(incident(wrt))
        page = c.get("/incidents/1")
        assert "Draft a fix" in page.text
        scripted_ai(monkeypatch, [("read_file", {"path": "src/server.js"}), ("propose_fix", PROPOSAL)])
        page = c.post("/incidents/1/fix", data={"csrf": csrf_of(page.text)})
        assert "Listen on the PORT Coolify provides" in page.text
        assert 'class="d-add">+app.listen(process.env.PORT' in page.text
        assert "Open draft pull request on acme/api" in page.text
        page = c.post("/incidents/1/fix/discard", data={"csrf": csrf_of(page.text)})
        assert "Draft a fix" in page.text
        assert "GitHub fix pull requests" in c.get("/settings").text
