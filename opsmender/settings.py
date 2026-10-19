"""Runtime settings stored in SQLite. Secret values are encrypted at rest."""

from __future__ import annotations

from typing import Any

from .db import Database, dumps, loads
from .security import SecretBox

DEFAULTS: dict[str, Any] = {
    # Coolify connection
    "coolify_url": "",
    "coolify_verify_tls": True,
    # Monitoring
    "poll_interval": 60,
    "metrics_interval": 60,
    "log_lines": 200,
    "log_max_bytes": 64_000,
    # Alert thresholds
    "cpu_threshold": 90,
    "mem_threshold": 90,
    "disk_threshold": 85,
    "sustain_samples": 5,
    "restart_threshold": 3,
    "disk_forecast_days": 7,
    # AI
    "ai_provider": "",          # preset id from opsmender.ai.providers.PRESETS, or ""
    "ai_base_url": "",
    "ai_model": "",
    "ai_auto_diagnose": False,
    # Actions
    "actions_enabled": False,
    # Use what's already configured in Coolify
    "auto_metrics": True,        # install the probe with Coolify's SSH keys (needs read:sensitive)
    "notify_via_coolify": True,  # send alerts through the team's Coolify notification channels
    # Notifications
    "notify_on_resolve": True,
}

# Keys whose values are encrypted with SecretBox.
SECRET_KEYS = {"coolify_token", "ai_api_key", "ssh_private_key", "webhook_token", "notify_webhook_url"}


class Settings:
    def __init__(self, db: Database, box: SecretBox):
        self.db = db
        self.box = box

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            return DEFAULTS.get(key, default)
        if key in SECRET_KEYS:
            return self.box.decrypt(row["value"]) or default
        return loads(row["value"], DEFAULTS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        stored = self.box.encrypt(str(value)) if key in SECRET_KEYS else dumps(value)
        self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, stored),
        )

    def delete(self, key: str) -> None:
        self.db.execute("DELETE FROM settings WHERE key = ?", (key,))

    def has(self, key: str) -> bool:
        return self.db.one("SELECT 1 FROM settings WHERE key = ?", (key,)) is not None

    def int(self, key: str) -> int:
        try:
            return int(self.get(key))
        except (TypeError, ValueError):
            return int(DEFAULTS[key])

    def bool(self, key: str) -> bool:
        return bool(self.get(key))
