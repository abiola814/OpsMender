"""Small GitHub REST client for fix pull requests.

It reads a repository and, after the user confirms, commits to a *new* branch and opens a pull request.
It never pushes to an existing branch and never merges.
"""

from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

DEFAULT_API = "https://api.github.com"
_REPO = re.compile(r"^(?:[a-z][a-z0-9+.-]*://(?P<host>[^/]+)/|git@(?P<sshhost>[^:]+):)?"
                   r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


class GitHubError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def web_host(api_url: str) -> str:
    """github.com for the public API, the server's own host for GitHub Enterprise."""
    host = urlsplit(api_url or DEFAULT_API).hostname or "github.com"
    return "github.com" if host == "api.github.com" else host


def parse_repo(value: str | None, api_url: str = DEFAULT_API) -> tuple[str, str] | None:
    """(owner, repo) from what Coolify stores: owner/repo, an https URL or an SSH remote on the GitHub host."""
    match = _REPO.match((value or "").strip())
    if not match:
        return None
    host = match.group("host") or match.group("sshhost")
    if host and host.rsplit("@", 1)[-1].split(":")[0].lower() != web_host(api_url).lower():
        return None  # GitLab, Bitbucket, a self-hosted Gitea...
    return match.group("owner"), match.group("name")


class GitHubClient:
    def __init__(self, token: str, api_url: str = DEFAULT_API, transport: httpx.AsyncBaseTransport | None = None):
        self.api_url = (api_url or DEFAULT_API).rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.api_url, timeout=30.0, transport=transport,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "OpsMender"})

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(f"Cannot reach GitHub at {self.api_url}: {exc}") from exc
        if resp.status_code < 400:
            return resp.json() if resp.content else None
        try:
            data = resp.json()
            detail = data.get("message") or ""
            errors = "; ".join(str(e.get("message") or e) for e in data.get("errors") or [] if e)
            detail = f"{detail} ({errors})" if errors else detail
        except (ValueError, AttributeError):
            detail = resp.text[:200]
        if resp.status_code == 401:
            raise GitHubError("GitHub rejected the token (401).", 401)
        if resp.status_code == 404:
            raise GitHubError("Not found on GitHub, or the token can't see it (404).", 404)
        if resp.status_code == 403:
            raise GitHubError(f"GitHub refused: {detail or 'the token lacks permission'} (403).", 403)
        raise GitHubError(f"GitHub {method} {path} failed ({resp.status_code}): {detail}", resp.status_code)

    async def user(self) -> dict[str, Any]:
        return await self._call("GET", "/user")

    async def repo(self, owner: str, name: str) -> dict[str, Any]:
        return await self._call("GET", f"/repos/{owner}/{name}")

    async def branch_head(self, owner: str, name: str, branch: str) -> str:
        data = await self._call("GET", f"/repos/{owner}/{name}/git/ref/heads/{quote(branch, safe='/')}")
        return data["object"]["sha"]

    async def tree(self, owner: str, name: str, sha: str) -> dict[str, dict[str, Any]]:
        """Every file in the commit: path -> {size, mode}."""
        data = await self._call("GET", f"/repos/{owner}/{name}/git/trees/{sha}", params={"recursive": "1"})
        return {e["path"]: {"size": e.get("size") or 0, "mode": e.get("mode") or "100644"}
                for e in data.get("tree") or [] if e.get("type") == "blob"}

    async def file(self, owner: str, name: str, path: str, ref: str) -> str | None:
        """A text file's contents at ``ref``, or None if it doesn't exist there."""
        try:
            data = await self._call("GET", f"/repos/{owner}/{name}/contents/{quote(path)}", params={"ref": ref})
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(data, dict) or data.get("type") != "file":
            raise GitHubError(f"{path} is not a file")
        if data.get("encoding") != "base64":
            raise GitHubError(f"{path} is too large to read through the API")
        raw = base64.b64decode(data.get("content") or "")
        if b"\x00" in raw[:8000]:
            raise GitHubError(f"{path} is a binary file")
        return raw.decode("utf-8", "replace")

    async def commit_files(self, owner: str, name: str, base_sha: str, branch: str, message: str,
                           files: list[dict[str, str]]) -> str:
        """One commit on top of ``base_sha`` with the given full file contents, on a new ``branch``."""
        repo = f"/repos/{owner}/{name}/git"
        base_tree = (await self._call("GET", f"{repo}/commits/{base_sha}"))["tree"]["sha"]
        entries = []
        for f in files:
            blob = await self._call("POST", f"{repo}/blobs", json={"content": f["content"], "encoding": "utf-8"})
            entries.append({"path": f["path"], "mode": f.get("mode") or "100644", "type": "blob", "sha": blob["sha"]})
        tree = await self._call("POST", f"{repo}/trees", json={"base_tree": base_tree, "tree": entries})
        commit = await self._call("POST", f"{repo}/commits",
                                  json={"message": message, "tree": tree["sha"], "parents": [base_sha]})
        await self._call("POST", f"{repo}/refs", json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
        return commit["sha"]

    async def open_pull(self, owner: str, name: str, head: str, base: str, title: str, body: str) -> dict[str, Any]:
        payload = {"title": title, "head": head, "base": base, "body": body, "draft": True}
        try:
            return await self._call("POST", f"/repos/{owner}/{name}/pulls", json=payload)
        except GitHubError as exc:
            if exc.status != 422 or "draft" not in str(exc).lower():
                raise
            # Draft pull requests need a paid plan on private repositories.
            return await self._call("POST", f"/repos/{owner}/{name}/pulls", json={**payload, "draft": False})
