"""Login, logout, and first-run admin setup."""

from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, Depends, Request

from ...security import hash_password, verify_password
from ..common import current_user, flash, form_with_csrf, redirect, render, rt_of

router = APIRouter()

_ATTEMPTS: dict[str, list[float]] = {}
MAX_ATTEMPTS, WINDOW = 10, 900


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _rate_limited(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _ATTEMPTS.get(ip, []) if now - t < WINDOW]
    _ATTEMPTS[ip] = recent
    return len(recent) >= MAX_ATTEMPTS


def has_users(request: Request) -> bool:
    return bool(rt_of(request).db.scalar("SELECT COUNT(*) FROM users"))


@router.get("/login")
async def login_page(request: Request):
    if not has_users(request):
        return redirect("/setup")
    if request.session.get("user"):
        return redirect("/")
    return render(request, "login.html")


@router.post("/login")
async def login(request: Request):
    form = await form_with_csrf(request)
    ip = _client_ip(request)
    if _rate_limited(ip):
        flash(request, "Too many attempts. Wait 15 minutes and try again.", "error")
        return redirect("/login")
    row = rt_of(request).db.one("SELECT * FROM users WHERE username = ?", (form.get("username", "").strip(),))
    if not row or not verify_password(form.get("password", ""), row["password_hash"]):
        _ATTEMPTS.setdefault(ip, []).append(time.time())
        flash(request, "Wrong username or password.", "error")
        return redirect("/login")
    _ATTEMPTS.pop(ip, None)
    csrf = request.session.get("csrf")
    request.session.clear()
    request.session["user"] = row["username"]
    request.session["csrf"] = csrf or secrets.token_urlsafe(32)
    next_url = form.get("next") or "/"
    if not next_url.startswith("/") or next_url.startswith("//") or "\\" in next_url:
        next_url = "/"
    return redirect(next_url)


@router.post("/logout")
async def logout(request: Request, user: str = Depends(current_user)):
    await form_with_csrf(request)
    request.session.clear()
    return redirect("/login")


@router.get("/setup")
async def setup_page(request: Request):
    if has_users(request):
        return redirect("/login")
    return render(request, "setup.html")


@router.post("/setup")
async def setup(request: Request):
    if has_users(request):
        return redirect("/login")
    form = await form_with_csrf(request)
    rt = rt_of(request)
    code = request.app.state.setup_code
    if not code or not secrets.compare_digest(form.get("setup_code", "").strip(), code):
        flash(request, "The setup code doesn't match. Find it in the OpsMender container logs.", "error")
        return redirect("/setup")
    username = form.get("username", "").strip()
    password = form.get("password", "")
    if not username or len(password) < 10:
        flash(request, "Choose a username and a password of at least 10 characters.", "error")
        return redirect("/setup")
    if password != form.get("password_confirm", ""):
        flash(request, "Passwords don't match.", "error")
        return redirect("/setup")
    rt.db.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                  (username, hash_password(password), time.time()))
    rt.db.audit(username, "create_admin", "ok")
    request.app.state.setup_code = None
    request.session["user"] = username
    flash(request, "Welcome to OpsMender. Connect your Coolify instance to get started.", "success")
    return redirect("/settings")
