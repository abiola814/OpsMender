"""Outbound alerts to a generic webhook (works with Slack and Discord incoming webhooks)."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("opsmender.notify")


async def send(webhook_url: str | None, text: str) -> bool:
    if not webhook_url:
        return False
    payload = {"text": text, "content": text[:2000], "username": "OpsMender"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(webhook_url, json=payload)
        if resp.status_code >= 400:
            log.warning("notification webhook returned %s", resp.status_code)
            return False
        return True
    except httpx.HTTPError as exc:
        log.warning("notification webhook failed: %s", exc)
        return False
