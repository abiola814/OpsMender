"""Deterministic diagnosis: known failure signatures matched against a bounded log window.

Each rule explains what the pattern usually means and what to check. No AI involved.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .logs import clean_line, group_errors


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    category: str
    severity: str
    patterns: tuple[str, ...]
    explanation: str
    suggestions: tuple[str, ...]


@dataclass
class Finding:
    rule_id: str
    title: str
    category: str
    severity: str
    explanation: str
    suggestions: list[str]
    matches: int
    sample: str
    details: dict[str, Any] = field(default_factory=dict)


DB_PORTS = {"5432": "PostgreSQL", "3306": "MySQL/MariaDB", "6379": "Redis", "27017": "MongoDB",
            "9000": "ClickHouse/MinIO", "5672": "RabbitMQ", "11211": "Memcached"}

RULES: tuple[Rule, ...] = (
    Rule("db_connection_refused", "Dependency refused the connection", "dependency", "critical",
         (r"ECONNREFUSED", r"[Cc]onnection refused", r"could not connect to (the )?server",
          r"Can't connect to (MySQL|local MySQL) server", r"Error connecting to Redis",
          r"MongoNetworkError", r"SQLSTATE\[HY000\] \[2002\]"),
         "The app tried to reach another service (often a database or cache) and nothing accepted the "
         "connection. The dependency is down, still starting, on a different network, or the host/port is wrong.",
         ("Check that the dependency's container is running and healthy in Coolify.",
          "Confirm the host name in the connection string is the dependency's internal name, not localhost.",
          "Make sure both resources share a Docker network (same Coolify destination or 'Connect to predefined network').",
          "If the dependency restarts often, look at its own logs first.")),
    Rule("db_auth_failed", "Database rejected the credentials", "configuration", "critical",
         (r"password authentication failed", r"Access denied for user", r"Authentication failed",
          r"WRONGPASS", r"NOAUTH Authentication required", r"SASL authentication failed"),
         "The dependency is reachable but the username or password doesn't match.",
         ("Compare the app's DATABASE_URL (or equivalent) with the database's credentials in Coolify.",
          "If the database volume pre-dates a password change, the old password still applies.")),
    Rule("dns_failure", "Host name could not be resolved", "network", "critical",
         (r"ENOTFOUND", r"getaddrinfo", r"Name or service not known", r"Temporary failure in name resolution",
          r"no such host", r"Could not resolve host", r"UnknownHostException"),
         "A host name in the configuration doesn't resolve from inside the container.",
         ("Check spelling of the host name in environment variables.",
          "Internal Coolify services resolve by container/service name only on a shared Docker network.",
          "For external hosts, check the server's DNS (/etc/resolv.conf).")),
    Rule("timeout", "Requests to a dependency are timing out", "network", "warning",
         (r"ETIMEDOUT", r"timed out", r"TimeoutError", r"context deadline exceeded", r"Read timed out",
          r"upstream timed out"),
         "Something the app depends on is slow or unreachable (firewall, overloaded service, wrong port).",
         ("Check the dependency's CPU and memory on the Servers page.",
          "Verify firewall rules if the dependency is on another server.")),
    Rule("out_of_memory", "Process ran out of memory", "resources", "critical",
         (r"JavaScript heap out of memory", r"\bMemoryError\b", r"OutOfMemoryError", r"OOMKilled",
          r"Out of memory", r"Killed process", r"fatal error: runtime: out of memory",
          r"Allowed memory size of \d+ bytes exhausted", r"exit code:? 137", r"exited with code 137"),
         "The process hit its memory limit and was killed (exit code 137 usually means OOM).",
         ("Look at the container's memory trend on the server page to see if it is a leak or a spike.",
          "Raise the container memory limit in Coolify or add RAM/swap to the server.",
          "For Node.js builds, set NODE_OPTIONS=--max-old-space-size to a value below the limit.")),
    Rule("port_in_use", "Port already in use", "configuration", "critical",
         (r"EADDRINUSE", r"address already in use", r"port is already allocated", r"Bind for .* failed"),
         "Two processes want the same port, often a host port mapping that clashes with another container.",
         ("Remove fixed host port mappings unless you need them; Coolify's proxy routes by domain.",
          "Check other resources on the same server for the same published port.")),
    Rule("wrong_listen_port", "App is not listening where the proxy expects", "configuration", "warning",
         (r"no available server", r"502 Bad Gateway", r"connect\(\) failed .* while connecting to upstream",
          r"listening on (127\.0\.0\.1|localhost)"),
         "The proxy can't reach the app: it listens on a different port than 'Ports Exposes', or binds to "
         "127.0.0.1 instead of 0.0.0.0.",
         ("Make the app listen on 0.0.0.0, not localhost.",
          "Set 'Ports Exposes' in Coolify to the port the app actually listens on.")),
    Rule("missing_module", "Missing dependency or module", "build", "critical",
         (r"ModuleNotFoundError", r"ImportError", r"Cannot find module", r"Module not found",
          r"cannot find package", r"no required module provides package", r"ClassNotFoundException",
          r"LoadError", r"Class .* not found"),
         "Code imports a package that isn't installed in the image.",
         ("Add the package to your dependency file (package.json, requirements.txt, go.mod…) and commit the lockfile.",
          "Check whether it is a devDependency that is pruned in production builds.")),
    Rule("missing_env", "Required environment variable is missing", "configuration", "critical",
         (r"KeyError: '[A-Z0-9_]+'", r"(environment variable|env var)\S* .*(not set|missing|required|undefined)",
          r"Missing required (environment|env)", r"is not defined in the environment",
          r"must be set", r"Invalid environment variables"),
         "The app expects a configuration value that isn't set for this deployment.",
         ("Add the variable in Coolify → Environment Variables, then redeploy.",
          "If it is needed at build time, mark it 'Build Variable'.")),
    Rule("permission_denied", "Permission denied", "configuration", "warning",
         (r"EACCES", r"Permission denied", r"Operation not permitted", r"EPERM"),
         "The process can't read or write a file, socket, or port it needs, often a volume owned by a different user.",
         ("Check ownership of mounted volumes against the user the container runs as.",
          "Ports below 1024 need root or CAP_NET_BIND_SERVICE.")),
    Rule("disk_full", "Disk is full", "resources", "critical",
         (r"No space left on device", r"ENOSPC", r"disk quota exceeded", r"could not extend file"),
         "The server or volume has run out of space.",
         ("Check disk usage on the Servers page.",
          "Clean up unused Docker images and build cache (Coolify → Server → Cleanup).",
          "Enable Docker log rotation so container logs can't fill the disk.")),
    Rule("tls_error", "TLS / certificate problem", "network", "warning",
         (r"certificate verify failed", r"x509:", r"SSL routines", r"self[- ]signed certificate",
          r"CERT_HAS_EXPIRED", r"unable to get local issuer certificate", r"UNABLE_TO_VERIFY_LEAF_SIGNATURE"),
         "A TLS connection failed: expired, self-signed, or untrusted certificate, or missing CA bundle.",
         ("If the image is minimal (alpine/distroless), install ca-certificates.",
          "Check that the remote certificate is valid and not expired.")),
    Rule("migration_missing", "Database schema out of date", "application", "critical",
         (r"relation \"?[\w.]+\"? does not exist", r"no such table", r"Table '.*' doesn't exist",
          r"column \"?[\w.]+\"? does not exist", r"Unknown column", r"pending migrations?"),
         "The code expects tables or columns that the database doesn't have yet.",
         ("Run your migrations (add them to the pre-deploy or start command).",
          "If a deploy rolled back code but not the schema, check for incompatible migrations.")),
    Rule("build_failed", "Build step failed", "build", "critical",
         (r"npm ERR!", r"ERR_PNPM", r"error: failed to solve", r"failed to compute cache key",
          r"Dockerfile.*(not found|cannot be empty)", r"exit code: [1-9]", r"returned a non-zero code",
          r"Build failed", r"Oops something is not okay", r"nixpacks.*(error|failed)", r"error TS\d+",
          r"Compilation failed"),
         "The image build stopped with an error before the new version could start.",
         ("Open the deployment log and find the first error, not the last one.",
          "Reproduce locally with the same build command and Node/Python/Go version.",
          "Check that the base directory and Dockerfile path in Coolify are correct.")),
    Rule("healthcheck_failed", "Health check failing", "application", "warning",
         (r"unhealthy", r"health ?check (failed|timed out)", r"container .* is unhealthy"),
         "The container is running but its health check doesn't pass, so it may be taken out of rotation.",
         ("Check the health check path and port in Coolify match a route the app serves.",
          "Give slow-starting apps a longer start period.")),
    Rule("crash_loop", "Process keeps crashing on start", "application", "critical",
         (r"Back-off restarting", r"restarting \(\d+\)", r"exited with code [1-9]\d*", r"Process exited with status"),
         "The main process exits shortly after starting, so the container restarts in a loop.",
         ("Read the first error after start-up in the logs; the cause is usually above the exit line.",
          "Check the start command and that required environment variables exist.")),
    Rule("unhandled_exception", "Unhandled exception in the application", "application", "warning",
         (r"Traceback \(most recent call last\)", r"UnhandledPromiseRejection", r"Unhandled exception",
          r"panic:", r"Uncaught \w*Error", r"Fatal error:"),
         "Application code raised an error that nothing caught.",
         ("Use the stack trace sample below to find the failing file and line.",
          "If it started after a deployment, compare with the previous commit.")),
    Rule("rate_limited", "Upstream rate limiting", "dependency", "warning",
         (r"\b429\b.*(Too Many Requests|rate limit)", r"rate limit(ed)? exceeded", r"TooManyRequests"),
         "An external API is throttling requests from this app.",
         ("Add retries with backoff, or reduce request volume.",)),
)

_COMPILED = [(rule, [re.compile(p, re.I) for p in rule.patterns]) for rule in RULES]
_PORT_RE = re.compile(r":(\d{2,5})\b")


def analyze(text: str) -> dict[str, Any]:
    """Return rule findings and error groups for a window of log text."""
    lines = [clean_line(l) for l in text.splitlines() if l.strip()]
    findings: list[Finding] = []
    for rule, patterns in _COMPILED:
        hits = [line for line in lines if any(p.search(line) for p in patterns)]
        if not hits:
            continue
        finding = Finding(
            rule_id=rule.id, title=rule.title, category=rule.category, severity=rule.severity,
            explanation=rule.explanation, suggestions=list(rule.suggestions),
            # Build logs are read top-down (the first error is the cause); runtime logs bottom-up.
            matches=len(hits), sample=(hits[0] if rule.category == "build" else hits[-1])[:500],
        )
        if rule.id == "db_connection_refused":
            dependency = _guess_dependency(hits)
            if dependency:
                finding.details["dependency"] = dependency
                finding.title = f"{dependency} refused the connection"
        findings.append(finding)

    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (severity_rank.get(f.severity, 3), -f.matches))
    groups = group_errors(text)
    return {
        "findings": [asdict(f) for f in findings],
        "error_groups": [asdict(g) for g in groups],
        "lines_analyzed": len(lines),
        "headline": findings[0].title if findings else (groups[0].sample[:160] if groups else None),
    }


def _guess_dependency(lines: list[str]) -> str | None:
    for line in lines:
        for port in _PORT_RE.findall(line):
            if port in DB_PORTS:
                return DB_PORTS[port]
        lower = line.lower()
        for word, name in (("postgres", "PostgreSQL"), ("mysql", "MySQL/MariaDB"), ("redis", "Redis"),
                           ("mongo", "MongoDB"), ("rabbit", "RabbitMQ")):
            if word in lower:
                return name
    return None
