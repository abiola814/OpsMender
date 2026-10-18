"""Entry point: ``python -m opsmender`` or the ``opsmender`` console script."""

from __future__ import annotations

import logging

import uvicorn

from .config import load_config
from .web.app import create_app


def main() -> None:
    config = load_config()
    logging.basicConfig(level=config.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level=config.log_level,
                proxy_headers=True, forwarded_allow_ips="*", access_log=False)


if __name__ == "__main__":
    main()
