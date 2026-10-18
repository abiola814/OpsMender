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
        raise ProbeError(f"SSH to {host}:{port} failed: {exc or type(exc).__name__}") from exc

    stdout = result.stdout if isinstance(result.stdout, str) else (result.stdout or b"").decode("utf-8", "replace")
    stderr = result.stderr if isinstance(result.stderr, str) else (result.stderr or b"").decode("utf-8", "replace")
    if result.exit_status not in (0, None):
        message = stderr.strip() or stdout.strip()
        if "sudo" in message and "password" in message:
            message = "sudo rule missing; re-run the setup script."
        raise ProbeError(f"Probe exited with {result.exit_status}: {message[:300]}")
    return ProbeResult(host_key=fingerprint, output=stdout)


async def snapshot(host: str, port: int, private_pem: str, known_host_key: str | None) -> tuple[str, dict[str, Any]]:
    result = await run_probe(host, port, private_pem, known_host_key, "snapshot")
    line = result.output.strip().splitlines()[-1] if result.output.strip() else ""
    try:
        data = json.loads(line)
    except ValueError as exc:
        raise ProbeError(f"Probe returned invalid JSON: {line[:200]!r}") from exc
    return result.host_key, data


async def container_logs(host: str, port: int, private_pem: str, known_host_key: str | None,
                         container: str, lines: int) -> str:
    if not _NAME_RE.match(container):
        raise ProbeError("invalid container name")
    lines = max(1, min(int(lines), 1000))
    result = await run_probe(host, port, private_pem, known_host_key, f"logs {container} {lines}")
    return result.output
