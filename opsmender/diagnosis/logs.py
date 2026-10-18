"""Turn a window of raw log lines into compact, deduplicated error groups."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_TS_PREFIX = re.compile(
    r"^\s*(\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\]?\s*)"
)
_NORMALIZERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<id>"),
    (re.compile(r"\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"), "<time>"),
    (re.compile(r"\d+"), "<n>"),
]
_ERRORISH = re.compile(
    r"(?i)\b(error|err!|exception|fatal|panic|traceback|critical|failed|failure|refused|denied|"
    r"timeout|timed out|cannot|could not|unable to|killed|segfault|unhandled|uncaught)\b"
)
_STACK_LINE = re.compile(r"^\s+(at |File \"|\.\.\. \d+ more|Caused by)")


@dataclass
class ErrorGroup:
    fingerprint: str
    template: str
    sample: str
    count: int = 1
    context: list[str] = field(default_factory=list)


def clean_line(line: str) -> str:
    line = _ANSI.sub("", line).rstrip()
    return _TS_PREFIX.sub("", line)


def template_of(line: str) -> str:
    text = clean_line(line)
    for pattern, repl in _NORMALIZERS:
        text = pattern.sub(repl, text)
    return text.strip()[:240]


def fingerprint_of(line: str) -> str:
    return hashlib.sha1(template_of(line).encode()).hexdigest()[:12]


def bound(text: str, max_lines: int, max_bytes: int) -> str:
    """Keep the tail of ``text`` within both a line and a byte budget."""
    lines = text.splitlines()[-max_lines:]
    out: list[str] = []
    size = 0
    for line in reversed(lines):
        size += len(line.encode("utf-8", "replace")) + 1
        if size > max_bytes:
            break
        out.append(line)
    return "\n".join(reversed(out))


def group_errors(text: str, limit: int = 15) -> list[ErrorGroup]:
    groups: dict[str, ErrorGroup] = {}
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        line = clean_line(raw)
        if not line or not _ERRORISH.search(line) or _STACK_LINE.match(line):
            continue
        fp = fingerprint_of(line)
        group = groups.get(fp)
        if group:
            group.count += 1
            continue
        context = [clean_line(l) for l in lines[i + 1:i + 8] if _STACK_LINE.match(clean_line(l))]
        groups[fp] = ErrorGroup(fingerprint=fp, template=template_of(line), sample=line[:500], context=context)
    return sorted(groups.values(), key=lambda g: g.count, reverse=True)[:limit]
