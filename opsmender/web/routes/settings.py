"""Settings: Coolify connection, AI provider, thresholds, notifications, actions, account."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Request

from ...adapters.coolify import CoolifyClient, CoolifyError
from ...ai.providers import PRESETS, AIConfig, AIError, complete
from ...notify import Alert, coolify_channels, dispatch
from ...security import hash_password, verify_password
from ..common import current_user, flash, form_with_csrf, redirect, render, rt_of

router = APIRouter()

NUMERIC = {
    "poll_interval": (15, 3600), "metrics_interval": (15, 3600), "log_lines": (20, 1000),
    "log_max_bytes": (4000, 256000), "cpu_threshold": (10, 100), "mem_threshold": (10, 100),
    "disk_threshold": (10, 100), "sustain_samples": (1, 60), "restart_threshold": (1, 50),
    "disk_forecast_days": (1, 90),
}


async def check_access(client: CoolifyClient) -> list[dict]:
    """What this Coolify token can reach, so the UI can say what works automatically."""
    access = []

    async def probe(key: str, label: str, coro, judge):
        try:
            ok, detail = judge(await coro)
        except CoolifyError as exc:
            ok, detail = False, ("needs more permissions" if exc.status in (401, 403)
                                 else "not available in this Coolify version" if exc.status in (404, 405)
                                 else str(exc)[:120])
        access.append({"key": key, "label": label, "ok": ok, "detail": detail})

    await probe("inventory", "Servers, apps, services, databases", client.list_servers(),
                lambda servers: (True, f"{len(servers)} server(s)"))
    await probe("ssh_keys", "Automatic server metrics (uses Coolify's SSH keys)", client.private_keys(),
                lambda keys: (any(str(k.get("private_key") or "").startswith("-----BEGIN") for k in keys),
                              "ready" if any(str(k.get("private_key") or "").startswith("-----BEGIN") for k in keys)
                              else "give the token the read:sensitive permission"))
    await probe("notifications", "Your Coolify notification channels", client.notification_settings("discord"),
                lambda data: (data is not None, "ready" if data is not None else "not available in this Coolify version"))
    return access


def webhook_token(rt) -> str:
    token = rt.settings.get("webhook_token")
    if not token:
        token = secrets.token_urlsafe(24)
        rt.settings.set("webhook_token", token)
    return token


@router.get("/settings")
async def settings_page(request: Request, user: str = Depends(current_user)):
    rt = rt_of(request)
    s = rt.settings
    base = rt.config.base_url or str(request.base_url).rstrip("/")
    return render(
        request, "settings.html",
        s={k: s.get(k) for k in ("coolify_url", "coolify_verify_tls", "ai_provider", "ai_base_url", "ai_model",
                                 "ai_auto_diagnose", "actions_enabled", "notify_on_resolve", "auto_metrics",
                                 "notify_via_coolify", *NUMERIC)},
        access=s.get("coolify_access") or [],
        channels=await coolify_channels(rt) if s.get("coolify_token") else [],
        has_token=bool(s.get("coolify_token")), has_ai_key=bool(s.get("ai_api_key")),
        has_notify=bool(s.get("notify_webhook_url")),
        presets=list(PRESETS.values()),
        webhook_url=f"{base}/webhooks/coolify/{webhook_token(rt)}",
    )


@router.post("/settings/coolify")
async def save_coolify(request: Request, user: str = Depends(current_user)):
    form = await form_with_csrf(request)
    rt = rt_of(request)
    url = form.get("coolify_url", "").strip().rstrip("/")
    if url.endswith("/api/v1"):
        url = url[: -len("/api/v1")]
    token = form.get("coolify_token", "").strip() or rt.settings.get("coolify_token") or ""
    verify = form.get("coolify_verify_tls") == "on"
    if not url.startswith(("http://", "https://")):
        flash(request, "The Coolify URL must start with http:// or https://", "error")
        return redirect("/settings#coolify")
    client = CoolifyClient(url, token, verify_tls=verify)
    try:
        version = await client.version()
        servers = await client.list_servers()
        access = await check_access(client)
    except CoolifyError as exc:
        flash(request, f"Couldn't connect: {exc}", "error")
        return redirect("/settings#coolify")
    finally:
        await client.close()
    rt.settings.set("coolify_url", url)
    rt.settings.set("coolify_token", token)
    rt.settings.set("coolify_verify_tls", verify)
    rt.settings.set("coolify_access", access)
    rt.settings.set("auto_metrics", form.get("auto_metrics") == "on")
    rt.status.pop("coolify_channels", None)
    rt.status["coolify_version"] = version.strip('"')
    rt.db.audit(user, "save_coolify", "ok", detail=url)
    rt.wake.set()
    flash(request, f"Connected to Coolify {version.strip(chr(34))} with {len(servers)} server(s). Syncing now…", "success")
    return redirect("/settings#coolify")


@router.post("/settings/ai")
async def save_ai(request: Request, user: str = Depends(current_user)):
    form = await form_with_csrf(request)
    rt = rt_of(request)
    provider = form.get("ai_provider", "")
    if provider and provider not in PRESETS:
        flash(request, "Unknown provider.", "error")
        return redirect("/settings#ai")
    rt.settings.set("ai_provider", provider)
    rt.settings.set("ai_model", form.get("ai_model", "").strip() or (PRESETS[provider].default_model if provider else ""))
    rt.settings.set("ai_base_url", form.get("ai_base_url", "").strip())
    rt.settings.set("ai_auto_diagnose", form.get("ai_auto_diagnose") == "on")
    if form.get("ai_api_key", "").strip():
        rt.settings.set("ai_api_key", form["ai_api_key"].strip())
    if form.get("clear_key") == "on":
        rt.settings.delete("ai_api_key")
    rt.db.audit(user, "save_ai", "ok", detail=provider or "disabled")
    if form.get("test") == "1" and provider:
        cfg = AIConfig(provider=provider, model=rt.settings.get("ai_model"), api_key=rt.settings.get("ai_api_key"),
                       base_url=rt.settings.get("ai_base_url") or None)
        try:
            answer = await complete(cfg, "Reply with the single word: ready", system="You are a connectivity check.",
                                    max_tokens=64)
            flash(request, f"{cfg.preset.label} responded: {answer[:80]}", "success")
        except AIError as exc:
            flash(request, f"Test failed: {exc}", "error")
    else:
        flash(request, "AI settings saved." if provider else "AI diagnosis disabled.", "success")
    return redirect("/settings#ai")


@router.post("/settings/monitoring")
async def save_monitoring(request: Request, user: str = Depends(current_user)):
    form = await form_with_csrf(request)
    rt = rt_of(request)
    for key, (lo, hi) in NUMERIC.items():
        if key in form:
            try:
                rt.settings.set(key, max(lo, min(hi, int(form[key]))))
            except ValueError:
                pass
    rt.db.audit(user, "save_monitoring", "ok")
    flash(request, "Monitoring settings saved.", "success")
    return redirect("/settings#monitoring")


@router.post("/settings/notifications")
async def save_notifications(request: Request, user: str = Depends(current_user)):
    form = await form_with_csrf(request)
    rt = rt_of(request)
    url = form.get("notify_webhook_url", "").strip()
    if url:
        if not url.startswith("https://") and not url.startswith("http://"):
            flash(request, "Webhook URL must start with https://", "error")
            return redirect("/settings#notifications")
        rt.settings.set("notify_webhook_url", url)
    if form.get("clear_webhook") == "on":
        rt.settings.delete("notify_webhook_url")
    rt.settings.set("notify_on_resolve", form.get("notify_on_resolve") == "on")
    rt.settings.set("notify_via_coolify", form.get("notify_via_coolify") == "on")
    if form.get("test") == "1":
        results = await dispatch(rt, Alert(title="Test notification", body="If you can read this, OpsMender alerts "
                                           "reach you here.", severity="info", kind="test", url=rt.public_url()),
                                 force=True)
        if not results:
            flash(request, "No destinations: enable a channel in Coolify (Notifications) or add a webhook here.", "error")
        for dest, ok, detail in results:
            flash(request, f"{dest}: {detail}", "success" if ok else ("info" if ok is None else "error"))
    else:
        flash(request, "Notification settings saved.", "success")
    return redirect("/settings#notifications")


@router.post("/settings/actions")
async def save_actions(request: Request, user: str = Depends(current_user)):
    form = await form_with_csrf(request)
    rt = rt_of(request)
    enabled = form.get("actions_enabled") == "on"
    rt.settings.set("actions_enabled", enabled)
    rt.db.audit(user, "actions_enabled" if enabled else "actions_disabled", "ok")
    flash(request, "Actions enabled. Each one still asks for confirmation." if enabled else "Actions disabled.",
          "success")
    return redirect("/settings#actions")


@router.post("/settings/webhook/rotate")
async def rotate_webhook(request: Request, user: str = Depends(current_user)):
    await form_with_csrf(request)
    rt = rt_of(request)
    rt.settings.set("webhook_token", secrets.token_urlsafe(24))
    rt.db.audit(user, "rotate_webhook_token", "ok")
    flash(request, "Webhook URL regenerated. Update it in Coolify's notification settings.", "info")
    return redirect("/settings#webhook")


@router.post("/settings/password")
async def change_password(request: Request, user: str = Depends(current_user)):
    form = await form_with_csrf(request)
    rt = rt_of(request)
    row = rt.db.one("SELECT * FROM users WHERE username = ?", (user,))
    if not row or not verify_password(form.get("current", ""), row["password_hash"]):
        flash(request, "Current password is wrong.", "error")
    elif len(form.get("new", "")) < 10 or form.get("new") != form.get("confirm"):
        flash(request, "New passwords must match and be at least 10 characters.", "error")
    else:
        rt.db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(form["new"]), row["id"]))
        rt.db.audit(user, "change_password", "ok")
        flash(request, "Password changed.", "success")
    return redirect("/settings#account")
