import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake_coolify import FakeCoolify  # noqa: E402

from opsmender.adapters.coolify import CoolifyClient  # noqa: E402
from opsmender.config import Config  # noqa: E402
from opsmender.runtime import Runtime  # noqa: E402


def make_config(tmp_path: Path, **overrides) -> Config:
    values = dict(data_dir=tmp_path, secret_key="test-secret", host="127.0.0.1", port=0, base_url="",
                  secure_cookies=False, admin_user=None, admin_password=None, coolify_url=None,
                  coolify_token=None, log_level="warning")
    values.update(overrides)
    return Config(**values)


@pytest.fixture
def fake():
    return FakeCoolify()


@pytest.fixture
def rt(tmp_path, fake, monkeypatch):
    runtime = Runtime(make_config(tmp_path))
    runtime.settings.set("coolify_url", "https://coolify.test")
    runtime.settings.set("coolify_token", fake.token)
    client = CoolifyClient("https://coolify.test", fake.token, transport=fake.transport())
    monkeypatch.setattr(runtime, "coolify", lambda: client)
    yield runtime
    runtime.db.close()
