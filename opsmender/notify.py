"""Outbound alerts.

By default OpsMender sends through the notification channels the team already configured in Coolify
(Discord, Slack, Telegram, Pushover, webhook, email), read from the Coolify API. Coolify has no "send"
endpoint, so OpsMender delivers the messages itself with those settings. An extra webhook can be added
in OpsMender's own settings.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Awaitable, Callable

import httpx

from .adapters.coolify import CoolifyError

log = logging.getLogger("opsmender.notify")

CHANNELS = ("discord", "slack", "telegram", "pushover", "webhook", "email")
LABELS = {"discord": "Discord", "slack": "Slack", "telegram": "Telegram", "pushover": "Pushover",
          "webhook": "Webhook", "email": "Email"}
# Incident kinds Coolify already notifies about itself, and the Coolify event toggle that covers each.
COOLIFY_COVERS = {"deployment_failed": "deployment_failure", "resource_down": "status_change",
                  "server_unreachable": "server_unreachable", "disk_pressure": "server_disk_usage"}
CACHE_SECONDS = 300
ICONS = {"critical": "🔴", "warning": "🟠", "info": "🔵", "resolved": "✅"}


@dataclass
class Alert:
    title: str
    body: str = ""
    severity: str = "info"     # critical | warning | info | resolved
    kind: str = "test"
    url: str | None = None

    def text(self, limit: int = 4000) -> str:
        parts = [f"{ICONS.get(self.severity, '')} OpsMender: {self.title}".strip()]
        if self.body:
            parts.append(self.body)
        if self.url:
            parts.append(self.url)
        return "\n".join(parts)[:limit]


@dataclass
class Channel:
    name: str
    settings: dict[str, Any]
    ready: bool
    problem: str | None = None

    @property
    def label(self) -> str:
        return LABELS[self.name]

    def covers(self, kind: str) -> bool:
        """True when Coolify itself already sends this kind of alert on this channel."""
        event = COOLIFY_COVERS.get(kind)
        return bool(event and self.settings.get(f"{event}_{self.name}_notifications"))


# Reading Coolify's channel settings

_REQUIRED = {
    "discord": ("discord_webhook_url",), "slack": ("slack_webhook_url",),
    "telegram": ("telegram_token", "telegram_chat_id"), "pushover": ("pushover_user_key", "pushover_api_token"),
    "webhook": ("webhook_url",),
}


async def coolify_channels(rt, refresh: bool = False) -> list[Channel]:
    """Enabled channels in Coolify, cached for a few minutes. Empty if Coolify isn't connected."""
    cached = rt.status.get("coolify_channels")
    if cached is not None and not refresh and time.time() - rt.status.get("coolify_channels_at", 0) < CACHE_SECONDS:
        return cached
    client = rt.coolify()
    channels: list[Channel] = []
    if client is not None:
        for name in CHANNELS:
            try:
                settings = await client.notification_settings(name)
            except CoolifyError as exc:
                log.info("Coolify %s notification settings unavailable: %s", name, exc)
                continue
            if not settings:
                continue
            if name == "email":
                channel = await _email_channel(client, settings)
                if channel:
                    channels.append(channel)
                continue
            if not settings.get(f"{name}_enabled"):
                continue
            missing = [f for f in _REQUIRED[name] if not settings.get(f)]
            channels.append(Channel(name, settings, not missing,
                                    "the Coolify token can't read this channel's secret (needs read:sensitive)"
                                    if missing else None))
    rt.status["coolify_channels"] = channels
    rt.status["coolify_channels_at"] = time.time()
    return channels


async def _email_channel(client, settings: dict[str, Any]) -> Channel | None:
    if not (settings.get("smtp_enabled") or settings.get("resend_enabled") or settings.get("use_instance_email_settings")):
        return None
    merged = dict(settings)
    if settings.get("use_instance_email_settings"):
        instance = await client.instance_email_settings()
        if not instance:
            return Channel("email", settings, False, "uses Coolify's instance email settings, which only a root-team "
                                                     "token can read")
        for key, value in instance.items():
            if key.startswith(("smtp_", "resend_")) and key != "smtp_recipients":
                merged[key] = value
    if not merged.get("smtp_recipients"):
        return Channel("email", merged, False, "no recipients configured in Coolify")
    if merged.get("resend_enabled") and merged.get("resend_api_key"):
        return Channel("email", merged, True)
    if merged.get("smtp_host") and (merged.get("smtp_password") or not merged.get("smtp_username")):
        return Channel("email", merged, True)
    return Channel("email", merged, False, "the Coolify token can't read the email password (needs read:sensitive)")


# Senders: each returns (ok, detail)

Sender = Callable[[dict[str, Any], Alert], Awaitable[tuple[bool, str]]]


async def _post(url: str, **kwargs: Any) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, **kwargs)
    except httpx.HTTPError as exc:
        return False, f"request failed: {exc}"
    return (resp.status_code < 400, "sent" if resp.status_code < 400 else f"HTTP {resp.status_code}: {resp.text[:120]}")


async def send_discord(s: dict[str, Any], a: Alert) -> tuple[bool, str]:
    return await _post(s["discord_webhook_url"], json={"content": a.text(2000), "username": "OpsMender"})


async def send_slack(s: dict[str, Any], a: Alert) -> tuple[bool, str]:
    return await _post(s["slack_webhook_url"], json={"text": a.text(3500)})


async def send_telegram(s: dict[str, Any], a: Alert) -> tuple[bool, str]:
    return await _post(f"https://api.telegram.org/bot{s['telegram_token']}/sendMessage",
                       json={"chat_id": s["telegram_chat_id"], "text": a.text(4000), "disable_web_page_preview": True})


async def send_pushover(s: dict[str, Any], a: Alert) -> tuple[bool, str]:
    data = {"token": s["pushover_api_token"], "user": s["pushover_user_key"], "title": f"OpsMender: {a.title}"[:250],
            "message": (a.body or a.title)[:1024], "priority": 1 if a.severity == "critical" else 0}
    if a.url:
        data["url"] = a.url
    return await _post("https://api.pushover.net/1/messages.json", data=data)


async def send_webhook_url(url: str, a: Alert) -> tuple[bool, str]:
    payload = {"source": "opsmender", "event": a.kind, "severity": a.severity, "title": a.title,
               "message": a.body, "url": a.url, "timestamp": int(time.time()),
               # Slack- and Discord-compatible fields, for incoming webhooks of those services.
               "text": a.text(3500), "content": a.text(2000)}
    return await _post(url, json=payload)


async def send_webhook(s: dict[str, Any], a: Alert) -> tuple[bool, str]:
    return await send_webhook_url(s["webhook_url"], a)


async def send_email(s: dict[str, Any], a: Alert) -> tuple[bool, str]:
    recipients = [r.strip() for r in str(s.get("smtp_recipients") or "").replace(";", ",").split(",") if r.strip()]
    subject = f"[OpsMender] {a.title}"
    from_addr = s.get("smtp_from_address") or "opsmender@localhost"
    sender = f"{s.get('smtp_from_name') or 'OpsMender'} <{from_addr}>"
    if s.get("resend_enabled") and s.get("resend_api_key"):
        return await _post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {s['resend_api_key']}"},
                           json={"from": sender, "to": recipients, "subject": subject, "text": a.text(10_000)})

    def deliver() -> None:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(recipients)
        msg.set_content(a.text(10_000))
        host, port = s["smtp_host"], int(s.get("smtp_port") or 587)
        timeout = int(s.get("smtp_timeout") or 30)
        encryption = str(s.get("smtp_encryption") or "").lower()
        context = ssl.create_default_context()
        if encryption == "ssl" or port == 465:
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
            if encryption in {"tls", "starttls"} or (encryption != "none" and port == 587):
                server.starttls(context=context)
        with server:
            if s.get("smtp_username"):
                server.login(s["smtp_username"], s.get("smtp_password") or "")
            server.send_message(msg)

    try:
        await asyncio.to_thread(deliver)
        return True, "sent"
    except (smtplib.SMTPException, OSError) as exc:
        return False, f"SMTP failed: {exc}"


SENDERS: dict[str, Sender] = {"discord": send_discord, "slack": send_slack, "telegram": send_telegram,
                              "pushover": send_pushover, "webhook": send_webhook, "email": send_email}


async def dispatch(rt, alert: Alert, force: bool = False) -> list[tuple[str, bool | None, str]]:
    """Send an alert everywhere it should go. Returns (destination, ok, detail); ok=None means skipped."""
    results: list[tuple[str, bool | None, str]] = []
    if rt.settings.bool("notify_via_coolify"):
        for channel in await coolify_channels(rt, refresh=force):
            if not channel.ready:
                results.append((f"Coolify {channel.label}", None, channel.problem or "not ready"))
            elif channel.covers(alert.kind) and not force:
                results.append((f"Coolify {channel.label}", None, "Coolify already sends this alert"))
            else:
                ok, detail = await SENDERS[channel.name](channel.settings, alert)
                results.append((f"Coolify {channel.label}", ok, detail))
    own = rt.settings.get("notify_webhook_url")
    if own:
        ok, detail = await send_webhook_url(own, alert)
        results.append(("OpsMender webhook", ok, detail))
    for dest, ok, detail in results:
        if ok is False:
            log.warning("notification to %s failed: %s", dest, detail)
    return results


async def send(webhook_url: str | None, text: str) -> bool:
    """Send plain text to a single webhook (kept for simple callers)."""
    if not webhook_url:
        return False
    ok, _ = await send_webhook_url(webhook_url, Alert(title=text))
    return ok
