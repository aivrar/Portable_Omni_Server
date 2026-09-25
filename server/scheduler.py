"""Periodic maintenance scheduler.

Runs as a single asyncio task started from the gateway lifespan. On each tick
it inspects ``maintenance.load_policy()``, decides which tasks are due
(``now - last_run >= cadence_s``), and submits them to the JobStore. The
``last_run`` map is persisted back to ``maintenance.json`` after each due task
is enqueued so a gateway restart picks up cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import maintenance
from state import jobs

logger = logging.getLogger(__name__)

_RAW_TICK = os.environ.get("OMNI_MAINTENANCE_TICK_S", "600")
try:
    TICK_SECONDS = max(30, int(_RAW_TICK))
except (TypeError, ValueError):
    TICK_SECONDS = 600


class MaintenanceScheduler:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        loop = asyncio.get_running_loop()
        self._stop.clear()
        self._task = loop.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        logger.info("Maintenance scheduler started (tick=%ds)", TICK_SECONDS)
        while not self._stop.is_set():
            try:
                self._tick_once()
            except Exception as e:
                logger.exception("Maintenance tick failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                continue

    def _tick_once(self) -> None:
        state = maintenance.load_policy()
        policy = state.get("policy", {})
        last_run = state.get("last_run", {})
        now = time.time()
        changed = False
        for task, cfg in policy.items():
            cadence = float(cfg.get("cadence_s", 0))
            if cadence <= 0:
                continue
            last = float(last_run.get(task, 0))
            if (now - last) < cadence:
                continue
            try:
                fn = maintenance.build_task_callable(task, cfg)
            except KeyError:
                logger.warning("Unknown maintenance task in policy: %s", task)
                continue
            try:
                job = jobs.enqueue_callable(
                    kind="maintenance",
                    fn=fn,
                    meta={"task": task, "scheduled": True},
                    active_key=f"maintenance:{task}",
                )
                last_run[task] = now
                changed = True
                logger.info("Enqueued scheduled task '%s' as job %s",
                            task, job.job_id)
            except Exception as e:
                logger.warning("Could not enqueue task %s: %s", task, e)
        if changed:
            # Persist ONLY the override subset (keys differing from DEFAULTS)
            # plus the run-bookkeeping. ``policy`` here is the fully-merged
            # DEFAULTS+overrides dict from load_policy(); writing it verbatim
            # would freeze the current defaults and mask future DEFAULTS
            # changes. load_policy() re-merges overrides onto fresh DEFAULTS,
            # so storing just the diff keeps unmodified keys tracking DEFAULTS.
            overrides: dict = {}
            for task, cfg in policy.items():
                base = maintenance.DEFAULTS.get(task, {})
                diff = {k: v for k, v in cfg.items()
                        if k not in base or base[k] != v}
                if diff:
                    overrides[task] = diff
            try:
                maintenance.save_policy(
                    {"policy": overrides, "last_run": last_run})
            except Exception as e:
                logger.warning("save_policy failed: %s", e)


_scheduler: MaintenanceScheduler | None = None


def get_scheduler() -> MaintenanceScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = MaintenanceScheduler()
    return _scheduler
