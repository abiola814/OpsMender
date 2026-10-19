"""Install the probe on a server automatically, using the SSH key Coolify already has for it.

Coolify's key is fetched from the Coolify API, used for one SSH session to run the setup script, and
never stored. Afterwards OpsMender connects with its own key, which can only run the read-only probe.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..adapters.coolify import CoolifyError
from ..db import loads
from ..runtime import Runtime
from . import ssh
from .disk import PROBE_VERSION
from .installer import install_script

log = logging.getLogger("opsmender.autosetup")
LOCAL_HOSTS = {"host.docker.internal", "localhost", "127.0.0.1", "::1"}
RETRY_AFTER = 6 * 3600


class SetupError(Exception):
    pass


def ensure_keypair(rt: Runtime) -> str:
    """OpsMender's own probe key; returns its public half."""
    private = rt.settings.get("ssh_private_key")
    if not private:
        private, _ = ssh.generate_keypair()
        rt.settings.set("ssh_private_key", private)
    return ssh.public_key_of(private)


def probe_host_for(server: dict[str, Any]) -> str:
    if server.get("probe_host"):
        return server["probe_host"]
    return "host.docker.internal" if (server.get("ip") or "") in LOCAL_HOSTS else server.get("ip") or ""


async def usable_keys(rt: Runtime) -> list[dict[str, Any]]:
    client = rt.coolify()
    if client is None:
        raise SetupError("Coolify isn't connected.")
    try:
        keys = [k for k in await client.private_keys() if not k.get("is_git_related")]
    except CoolifyError as exc:
        raise SetupError(f"Couldn't list SSH keys from Coolify: {exc}") from exc
    if not keys:
        raise SetupError("Coolify returned no server SSH keys for this token.")
    readable = [k for k in keys if str(k.get("private_key") or "").startswith("-----BEGIN")]
    if not readable:
        raise SetupError("The Coolify token can't read SSH keys. Give it the read:sensitive permission "
                         "(or set metrics up manually).")
    return readable


async def setup_server(rt: Runtime, server: dict[str, Any], actor: str = "system") -> str:
    """Install or update the probe on one server. Returns a short success message."""
    keys = await usable_keys(rt)
    keys.sort(key=lambda k: k.get("id") != server.get("private_key_id"))  # the server's own key first
    host, port = probe_host_for(server), server.get("port") or 22
    user = server.get("ssh_user") or "root"
    script = install_script(ensure_keypair(rt))
    rt.db.execute("UPDATE servers SET auto_setup_at = ? WHERE uuid = ?", (time.time(), server["uuid"]))
    last_error: Exception | None = None
    for key in keys:
        try:
            host_key, _ = await ssh.run_setup(host, port, user, key["private_key"], script)
            break
        except ssh.KeyRejected as exc:
            last_error = exc
            continue
        except ssh.ProbeError as exc:
            _fail(rt, server, actor, str(exc))
            raise SetupError(str(exc)) from exc
    else:
        message = f"None of Coolify's SSH keys were accepted by {user}@{host}. ({last_error})"
        _fail(rt, server, actor, message)
        raise SetupError(message)

    # The probe runs on the same sshd, so the host key seen now is the one to pin.
    rt.db.execute(
        "UPDATE servers SET metrics_enabled = 1, probe_host = ?, probe_port = ?, host_key = ?,"
        " auto_setup_error = NULL, probe_failures = 0 WHERE uuid = ?",
        (host, port, host_key, server["uuid"]),
    )
    rt.db.audit(actor, "auto_setup_metrics", "ok", "server", server["uuid"], server["name"], f"{user}@{host}:{port}")
    rt.db.add_event("metrics_setup", f"Metrics set up automatically on {server['name']}", "info")
    return f"Probe installed on {server['name']} using Coolify's SSH key (not stored)."


def _fail(rt: Runtime, server: dict[str, Any], actor: str, message: str) -> None:
    rt.db.execute("UPDATE servers SET auto_setup_error = ? WHERE uuid = ?", (message, server["uuid"]))
    rt.db.audit(actor, "auto_setup_metrics", "error", "server", server["uuid"], server["name"], message)


DISABLED_BY_USER = "Metrics were turned off by a user."


def needs_setup(server: dict[str, Any], current_probe: int | None) -> bool:
    """New servers without metrics, failed attempts after a pause, and servers with an outdated probe."""
    if server.get("reachable") == 0 or server.get("auto_setup_error") == DISABLED_BY_USER:
        return False
    recently_failed = bool(server.get("auto_setup_error")) and \
        (server.get("auto_setup_at") or 0) > time.time() - RETRY_AFTER
    if recently_failed:
        return False
    if not server.get("metrics_enabled"):
        return True
    return current_probe is not None and current_probe < PROBE_VERSION


async def setup_pending(rt: Runtime) -> None:
    """Called after each sync when automatic setup is on."""
    if not rt.settings.bool("auto_metrics") or rt.coolify() is None:
        return
    try:
        await _setup_pending(rt)
    except Exception:  # noqa: BLE001 - runs as a background task; never let it die silently
        log.exception("automatic metrics setup crashed")


async def _setup_pending(rt: Runtime) -> None:
    for server in rt.db.all("SELECT * FROM servers"):
        current = loads(server.get("current_metrics"), {}) or {}
        if not needs_setup(server, current.get("probe_v") if current else None):
            continue
        try:
            log.info("setting up metrics on %s", server["name"])
            await setup_server(rt, server)
        except SetupError as exc:
            log.info("automatic metrics setup skipped for %s: %s", server["name"], exc)
            if "read:sensitive" in str(exc) or "no server SSH keys" in str(exc):
                return  # same answer for every server
