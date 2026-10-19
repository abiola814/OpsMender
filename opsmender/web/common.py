"""Shared web helpers: templates, auth, CSRF, flash messages, formatting filters."""

from __future__ import annotations

import html
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Undefined
from markupsafe import Markup

from .. import __version__
from ..adapters.base import health_of
from ..metrics.disk import human as dsize
from ..runtime import Runtime

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class LoginRequired(Exception):
    pass


class CSRFError(Exception):
    pass


def rt_of(request: Request) -> Runtime:
    return request.app.state.rt


def current_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise LoginRequired()
    rt = rt_of(request)
    if not rt.db.one("SELECT 1 FROM users WHERE username = ?", (user,)):
        request.session.clear()
        raise LoginRequired()
    return user


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def form_with_csrf(request: Request) -> dict[str, str]:
    form = await request.form()
    expected = request.session.get("csrf")
    supplied = str(form.get("csrf", ""))
    if not expected or not secrets.compare_digest(expected, supplied):
        raise CSRFError()
    return {k: str(v) for k, v in form.items()}


def flash(request: Request, message: str, level: str = "info") -> None:
    request.session.setdefault("flash", []).append([level, message])


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def render(request: Request, template: str, **context: Any):
    rt = rt_of(request)
    if not rt.config.base_url:
        rt.status["public_url"] = str(request.base_url).rstrip("/")  # remembered for notification links
    flashes = request.session.pop("flash", [])
    open_counts = {r["severity"]: r["n"] for r in rt.db.all(
        "SELECT severity, COUNT(*) AS n FROM incidents WHERE status = 'open' GROUP BY severity")}
    base = {
        "request": request,
        "user": request.session.get("user"),
        "csrf": csrf_token(request),
        "flashes": flashes,
        "version": __version__,
        "status": rt.status,
        "self_usage": rt.self_usage(),
        "open_counts": open_counts,
        "coolify_configured": bool(rt.settings.get("coolify_url") and rt.settings.get("coolify_token")),
        "path": request.url.path,
    }
    base.update(context)
    return TEMPLATES.TemplateResponse(request, template, base)


# Filters

def ago(ts: Any) -> str:
    if ts in (None, ""):
        return "never"
    try:
        value = float(ts)
    except (TypeError, ValueError):
        value = _parse_iso(str(ts))
        if value is None:
            return str(ts)
    delta = time.time() - value
    if delta < 0:
        return "just now"
    for unit, seconds in (("d", 86400), ("h", 3600), ("m", 60)):
        if delta >= seconds:
            return f"{int(delta // seconds)}{unit} ago"
    return f"{int(delta)}s ago"


def when(ts: Any) -> str:
    try:
        value = float(ts)
    except (TypeError, ValueError):
        value = _parse_iso(str(ts)) if ts else None
    if value is None:
        return ""
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _parse_iso(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def size(value: Any) -> str:
    if value in (None, ""):
        return "–"
    n = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def pct(value: Any, digits: int = 0) -> str:
    # None, or a field missing from older stored data (Jinja Undefined raises its own error on float()).
    if value is None or isinstance(value, Undefined):
        return "–"
    try:
        return f"{float(value):.{digits}f}%"
    except (TypeError, ValueError):
        return "–"


def level_of(value: float | None, warn: float, crit: float) -> str:
    if value is None:
        return "muted"
    if value >= crit:
        return "bad"
    if value >= warn:
        return "warn"
    return "good"


def chart(points: list[dict[str, Any]], key: str, height: int = 64, max_value: float = 100.0) -> Markup:
    """Inline SVG area chart for a 0–max series. No JavaScript, scales to its container."""
    values = [(p["ts"], p.get(key)) for p in points if p.get(key) is not None]
    if len(values) < 2:
        return Markup('<div class="chart-empty">Collecting data…</div>')
    t0, t1 = values[0][0], values[-1][0]
    span = (t1 - t0) or 1
    w = 300.0
    coords = [((t - t0) / span * w, height - min(max(v, 0), max_value) / max_value * (height - 2) - 1) for t, v in values]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = f"0,{height} " + line + f" {w},{height}"
    latest = values[-1][1]
    peak = max(v for _, v in values)
    return Markup(
        f'<svg class="chart" viewBox="0 0 {w:.0f} {height}" preserveAspectRatio="none" role="img" '
        f'aria-label="{html.escape(key)} latest {latest:.0f}, peak {peak:.0f}">'
        f'<polygon class="chart-area" points="{area}"/>'
        f'<polyline class="chart-line" points="{line}" vector-effect="non-scaling-stroke"/></svg>'
    )


def health(status: str | None) -> str:
    return health_of(status)


_MD_INLINE = [
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\w)"), r"<em>\1</em>"),
]


def markdown(text: str | None) -> Markup:
    """Small, safe Markdown subset for AI answers: headings, lists, code blocks, inline code, bold."""
    if not text:
        return Markup("")
    out: list[str] = []
    in_code = False
    list_tag: str | None = None
    para: list[str] = []

    def inline(s: str) -> str:
        s = html.escape(s)
        for pattern, repl in _MD_INLINE:
            s = pattern.sub(repl, s)
        return s

    def flush_para() -> None:
        if para:
            out.append("<p>" + " ".join(inline(p) for p in para) + "</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            flush_para()
            close_list()
            out.append("</code></pre>" if in_code else "<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            out.append(html.escape(raw) + "\n")
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)", line)
        bullet = re.match(r"^\s*[-*]\s+(.*)", line)
        numbered = re.match(r"^\s*\d+[.)]\s+(.*)", line)
        if heading:
            flush_para()
            close_list()
            level = min(len(heading.group(1)) + 2, 6)
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
        elif bullet or numbered:
            flush_para()
            tag = "ul" if bullet else "ol"
            if list_tag != tag:
                close_list()
                out.append(f"<{tag}>")
                list_tag = tag
            out.append(f"<li>{inline((bullet or numbered).group(1))}</li>")
        elif not line.strip():
            flush_para()
            close_list()
        else:
            close_list()
            para.append(line.strip())
    flush_para()
    close_list()
    if in_code:
        out.append("</code></pre>")
    return Markup("\n".join(out))


TEMPLATES.env.filters.update(ago=ago, when=when, size=size, dsize=dsize, pct=pct, chart=chart, health=health,
                             markdown=markdown)
TEMPLATES.env.globals.update(level_of=level_of)
