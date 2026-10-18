"""Receiver for Coolify notification webhooks. Any event triggers an immediate sync."""

from __future__ import annotations

import json
import secrets

from fastapi import APIRouter, HTTPException, Request

from ..common import rt_of

router = APIRouter()
MAX_BODY = 64_000


@router.post("/webhooks/coolify/{token}")
async def coolify_webhook(request: Request, token: str):
    rt = rt_of(request)
    expected = rt.settings.get("webhook_token")
    if not expected or not secrets.compare_digest(token, expected):
        raise HTTPException(404)
    body = (await request.body())[:MAX_BODY]
    message = "Coolify notification received"
    try:
        data = json.loads(body or b"{}")
        if isinstance(data, dict):
            parts = [str(data[k]) for k in ("event", "title", "message", "description") if data.get(k)]
            if parts:
                message = "Coolify: " + " · ".join(parts)[:400]
    except ValueError:
        if body:
            message = "Coolify: " + body.decode("utf-8", "replace")[:400]
    rt.db.add_event("coolify_webhook", message, "info")
    rt.wake.set()
    return {"ok": True}
