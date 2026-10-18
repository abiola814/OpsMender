"""Background loops. Each one is independent so a slow probe never delays a Coolify sync."""

from __future__ import annotations

import asyncio
import logging

from ..adapters.coolify import CoolifyError
from ..metrics.store import rollup_and_prune
from ..runtime import Runtime
from .metrics import check_disk_forecasts, poll_all
from .sync import backfill_deployments, sync

log = logging.getLogger("opsmender.scheduler")


async def _sync_loop(rt: Runtime) -> None:
    backfilled = False
    while True:
        try:
            await sync(rt)
            if not backfilled and rt.status.get("last_sync_error") is None and rt.coolify() is not None:
                await backfill_deployments(rt)
                backfilled = True
        except CoolifyError as exc:
            log.warning("sync failed: %s", exc)
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("sync crashed")
        rt.wake.clear()
        try:
            await asyncio.wait_for(rt.wake.wait(), timeout=max(15, rt.settings.int("poll_interval")))
        except asyncio.TimeoutError:
            pass


async def _metrics_loop(rt: Runtime) -> None:
    while True:
        try:
            await poll_all(rt)
        except Exception:  # noqa: BLE001
            log.exception("metrics poll crashed")
        await asyncio.sleep(max(15, rt.settings.int("metrics_interval")))


async def _maintenance_loop(rt: Runtime) -> None:
    await asyncio.sleep(120)
    while True:
        try:
            rollup_and_prune(rt.db)
            check_disk_forecasts(rt)
        except Exception:  # noqa: BLE001
            log.exception("maintenance crashed")
        await asyncio.sleep(3600)


def start(rt: Runtime) -> list[asyncio.Task[None]]:
    return [asyncio.create_task(loop(rt), name=loop.__name__) for loop in (_sync_loop, _metrics_loop, _maintenance_loop)]
