"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from ..config import Config
from ..monitor import scheduler
from ..runtime import Runtime
from ..security import hash_password
from .common import TEMPLATES, CSRFError, LoginRequired
from .routes import ROUTERS

log = logging.getLogger("opsmender")


def _bootstrap(rt: Runtime, app: FastAPI) -> None:
    cfg = rt.config
    if not rt.db.scalar("SELECT COUNT(*) FROM users"):
        if cfg.admin_user and cfg.admin_password:
            rt.db.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                          (cfg.admin_user, hash_password(cfg.admin_password), time.time()))
            log.info("Created admin user %r from environment", cfg.admin_user)
        else:
            app.state.setup_code = secrets.token_hex(4).upper()
            log.warning("=" * 64)
            log.warning("OpsMender first-run setup code: %s", app.state.setup_code)
            log.warning("Open the OpsMender URL and enter this code to create the admin account.")
            log.warning("=" * 64)
    if cfg.coolify_url and cfg.coolify_token and not rt.settings.get("coolify_token"):
        rt.settings.set("coolify_url", cfg.coolify_url.rstrip("/"))
        rt.settings.set("coolify_token", cfg.coolify_token)


def create_app(config: Config, start_background: bool = True) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = Runtime(config)
        app.state.rt = rt
        app.state.setup_code = None
        _bootstrap(rt, app)
        tasks = scheduler.start(rt) if start_background else []
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await rt.close()

    app = FastAPI(title="OpsMender", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=config.secret_key, session_cookie="opsmender_session",
                       max_age=7 * 24 * 3600, same_site="strict", https_only=config.secure_cookies)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
        )
        return response

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired):
        target = "/login" if request.app.state.rt.db.scalar("SELECT COUNT(*) FROM users") else "/setup"
        return RedirectResponse(target, status_code=303)

    @app.exception_handler(CSRFError)
    async def csrf_failed(request: Request, exc: CSRFError):
        return HTMLResponse("<h1>Form expired</h1><p>Go back, reload the page and try again.</p>", status_code=403)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404 and request.session.get("user"):
            return TEMPLATES.TemplateResponse(request, "error.html", {"request": request, "message": exc.detail,
                                                                     "user": request.session.get("user")},
                                              status_code=404)
        return HTMLResponse(f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>", status_code=exc.status_code)

    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    for router in ROUTERS:
        app.include_router(router)
    return app
