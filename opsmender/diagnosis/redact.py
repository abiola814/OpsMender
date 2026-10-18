"""Strip secrets from log text before it is stored as evidence or sent to an AI provider."""

from __future__ import annotations

import re

MASK = "[REDACTED]"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # PEM private keys
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), MASK),
    # Credentials inside URLs: scheme://user:password@host
    (re.compile(r"(?P<pre>\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(?P<post>@)"), r"\g<pre>" + MASK + r"\g<post>"),
    # Authorization headers and bearer tokens
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)(bearer|basic|token)?\s*[A-Za-z0-9._~+/=-]{8,}"), r"\1\2 " + MASK),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"), "Bearer " + MASK),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), MASK),
    # Well-known key formats
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), MASK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), MASK),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"), MASK),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), MASK),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), MASK),
    # key=value / key: value where the key name looks sensitive
    (re.compile(
        r"(?i)(?P<key>\b[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
        r"client[_-]?secret|credentials?|auth)[\w.-]*\b[\"']?\s*[:=]\s*[\"']?)(?P<val>[^\s\"',;&]{3,})"
    ), r"\g<key>" + MASK),
]


def redact(text: str) -> str:
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text
