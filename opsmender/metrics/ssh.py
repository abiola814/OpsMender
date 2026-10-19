"""Run the probe on a server over SSH, pinning the host key on first use."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

import asyncssh

_NAME_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*$")


class ProbeError(Exception):
    pass


class HostKeyMismatch(ProbeError):
    pass


class KeyRejected(ProbeError):
    pass


@dataclass
class ProbeResult:
    host_key: str
    output: str


def generate_keypair() -> tuple[str, str]:
    key = asyncssh.generate_private_key("ssh-ed25519", comment="opsmender")
    return key.export_private_key().decode(), key.export_public_key().decode().strip()


def public_key_of(private_pem: str) -> str:
    return asyncssh.import_private_key(private_pem).export_public_key().decode().strip()


async def run_probe(host: str, port: int, private_pem: str, known_host_key: str | None,
                    request: str = "snapshot", username: str = "opsmender",
                    timeout: float = 20.0) -> ProbeResult:
    key = asyncssh.import_private_key(private_pem)
    try:
        async with asyncssh.connect(host, port=port, username=username, client_keys=[key],
                                    known_hosts=None, agent_path=None, connect_timeout=10,
                                    login_timeout=10) as conn:
            server_key = conn.get_server_host_key()
            fingerprint = server_key.get_fingerprint("sha256") if server_key else ""
            if known_host_key and fingerprint != known_host_key:
                raise HostKeyMismatch(
                    f"Host key changed (expected {known_host_key}, got {fingerprint}). "
                    "If the server was rebuilt, reset the host key in OpsMender."
                )
            result = await asyncio.wait_for(conn.run(request, check=False), timeout=timeout)
    except HostKeyMismatch:
        raise
    except asyncssh.PermissionDenied as exc:
        raise ProbeError("SSH key was rejected. Did the setup script run on this server?") from exc
    except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
        raise ProbeError(f"SSH to {host}:{port} failed: {str(exc) or type(exc).__name__}") from exc

    stdout = result.stdout if isinstance(result.stdout, str) else (result.stdout or b"").decode("utf-8", "replace")
    stderr = result.stderr if isinstance(result.stderr, str) else (result.stderr or b"").decode("utf-8", "replace")
    if result.exit_status not in (0, None):
        message = stderr.strip() or stdout.strip()
        if "sudo" in message and "password" in message:
            message = "sudo rule missing; re-run the setup script."
        raise ProbeError(f"Probe exited with {result.exit_status}: {message[:300]}")
    return ProbeResult(host_key=fingerprint, output=stdout)


async def run_setup(host: str, port: int, username: str, private_pem: str, script: str,
                    timeout: float = 180.0) -> tuple[str, str]:
    """Run a setup script on a server as ``username`` (through sudo if not root). Returns (host key, output).

    Used once, with the SSH key Coolify already has for the server, to install the probe.
    """
    key = asyncssh.import_private_key(private_pem)
    command = "sh -s" if username == "root" else "sudo -n sh -s"
    try:
        async with asyncssh.connect(host, port=port, username=username, client_keys=[key], known_hosts=None,
                                    agent_path=None, connect_timeout=10, login_timeout=10) as conn:
            server_key = conn.get_server_host_key()
            fingerprint = server_key.get_fingerprint("sha256") if server_key else ""
            result = await asyncio.wait_for(conn.run(command, input=script, check=False), timeout=timeout)
    except asyncssh.PermissionDenied as exc:
        raise KeyRejected(f"{username}@{host} rejected the key") from exc
    except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
        raise ProbeError(f"SSH to {host}:{port} failed: {str(exc) or type(exc).__name__}") from exc
    output = (result.stdout or "") + (result.stderr or "")
    if result.exit_status not in (0, None):
        if "password is required" in output or "a terminal is required" in output:
            raise ProbeError(f"{username} needs passwordless sudo to install the probe.")
        raise ProbeError(f"Setup exited with {result.exit_status}: {output.strip()[-300:]}")
    return fingerprint, output


async def snapshot(host: str, port: int, private_pem: str, known_host_key: str | None) -> tuple[str, dict[str, Any]]:
    result = await run_probe(host, port, private_pem, known_host_key, "snapshot")
    line = result.output.strip().splitlines()[-1] if result.output.strip() else ""
    try:
        data = json.loads(line)
    except ValueError as exc:
        raise ProbeError(f"Probe returned invalid JSON: {line[:200]!r}") from exc
    return result.host_key, data


async def disk(host: str, port: int, private_pem: str, known_host_key: str | None) -> dict[str, Any]:
    # `docker system df` sizes every volume, which can take a while on busy hosts.
    result = await run_probe(host, port, private_pem, known_host_key, "disk", timeout=90.0)
    line = result.output.strip().splitlines()[-1] if result.output.strip() else ""
    try:
        return json.loads(line)
    except ValueError as exc:
        raise ProbeError(f"Probe returned invalid JSON: {line[:200]!r}") from exc


async def container_logs(host: str, port: int, private_pem: str, known_host_key: str | None,
                         container: str, lines: int) -> str:
    if not _NAME_RE.match(container):
        raise ProbeError("invalid container name")
    lines = max(1, min(int(lines), 1000))
    result = await run_probe(host, port, private_pem, known_host_key, f"logs {container} {lines}")
    return result.output
