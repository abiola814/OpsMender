"""Process configuration from environment variables.

Everything a user changes at runtime (Coolify connection, AI provider, thresholds) lives in the
database instead; see ``opsmender.settings``.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    data_dir: Path
    secret_key: str
    host: str
    port: int
    base_url: str
    secure_cookies: bool
    admin_user: str | None
    admin_password: str | None
    coolify_url: str | None
    coolify_token: str | None
    log_level: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "opsmender.db"


def _load_secret_key(data_dir: Path) -> str:
    from_env = os.environ.get("OPSMENDER_SECRET_KEY", "").strip()
    if from_env:
        return from_env
    key_file = data_dir / "secret.key"
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_urlsafe(48)
    key_file.write_text(key)
    key_file.chmod(0o600)
    return key


def load_config() -> Config:
    data_dir = Path(os.environ.get("OPSMENDER_DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    base_url = os.environ.get("OPSMENDER_BASE_URL", "").strip().split(",")[0].rstrip("/")
    if base_url and not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url  # some Coolify versions provide a bare domain
    return Config(
        data_dir=data_dir,
        secret_key=_load_secret_key(data_dir),
        host=os.environ.get("OPSMENDER_HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", os.environ.get("OPSMENDER_PORT", "8000"))),
        base_url=base_url,
        secure_cookies=_bool(os.environ.get("OPSMENDER_SECURE_COOKIES"), base_url.startswith("https://")),
        admin_user=os.environ.get("OPSMENDER_ADMIN_USER") or None,
        admin_password=os.environ.get("OPSMENDER_ADMIN_PASSWORD") or None,
        coolify_url=os.environ.get("OPSMENDER_COOLIFY_URL") or None,
        coolify_token=os.environ.get("OPSMENDER_COOLIFY_TOKEN") or None,
        log_level=os.environ.get("OPSMENDER_LOG_LEVEL", "info"),
    )
