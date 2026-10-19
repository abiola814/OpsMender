"""Shared process state: database, settings, Coolify client, scheduler signals."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import psutil

from .adapters.coolify import CoolifyClient
from .config import Config
from .db import Database
from .security import SecretBox
from .settings import Settings

log = logging.getLogger("opsmender")


class Runtime:
    def __init__(self, config: Config, db: Database | None = None):
        self.config = config
        self.db = db or Database(config.db_path)
        self.box = SecretBox(config.secret_key)
        self.settings = Settings(self.db, self.box)
        self.wake = asyncio.Event()
        self.status: dict[str, Any] = {"last_sync_at": None, "last_sync_error": None, "coolify_version": None,
                                       "last_metrics_at": None, "started_at": time.time()}
        # Resources the user stopped through OpsMender: uuid -> suppress-until timestamp.
        self.suppressed: dict[str, float] = {}
        self.ai_inflight: set[int] = set()
        # Coolify resource uuid -> server uuid, rebuilt on each sync.
        self.server_map: dict[str, str] = {}
        self._coolify: CoolifyClient | None = None
        self._coolify_sig: tuple[str, str, bool] | None = None
        self._process = psutil.Process()
        self._process.cpu_percent(None)

    def coolify(self) -> CoolifyClient | None:
        url = (self.settings.get("coolify_url") or "").strip()
        token = self.settings.get("coolify_token") or ""
        verify = self.settings.bool("coolify_verify_tls")
        if not url or not token:
            return None
        sig = (url, token, verify)
        if self._coolify is None or sig != self._coolify_sig:
            old = self._coolify
            self._coolify = CoolifyClient(url, token, verify_tls=verify)
            self._coolify_sig = sig
            if old is not None:
                asyncio.ensure_future(old.close())
        return self._coolify

    def public_url(self) -> str | None:
        """OpsMender's own address, for links in notifications."""
        return self.config.base_url or self.status.get("public_url")

    def self_usage(self) -> dict[str, float]:
        try:
            return {"rss_mb": self._process.memory_info().rss / 1_048_576, "cpu_pct": self._process.cpu_percent(None)}
        except psutil.Error:
            return {"rss_mb": 0.0, "cpu_pct": 0.0}

    def is_suppressed(self, uuid: str) -> bool:
        until = self.suppressed.get(uuid)
        if until and until > time.time():
            return True
        self.suppressed.pop(uuid, None)
        return False

    async def close(self) -> None:
        if self._coolify is not None:
            await self._coolify.close()
        self.db.close()
