"""Optional background scan schedule. Scans only — deletions are never automatic.

The schedule is re-read from the settings store on every tick, so changing the
scan time in the web UI applies without a restart.
"""
from __future__ import annotations

import logging

from .db import Database

log = logging.getLogger(__name__)


def start_scheduler(store, db: Database, runner) -> object | None:
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        log.warning("apscheduler is not installed — scheduled scans are disabled")
        return None

    scheduler = BackgroundScheduler(timezone="UTC")
    state = {"hour": None, "minute": None, "enabled": None}

    def scheduled_scan() -> None:
        cfg = store.current()
        if not cfg.schedule.scan_enabled:
            return
        if not cfg.can_scan():
            log.warning("skipping scheduled scan: configuration is incomplete")
            return
        log.info("scheduled scan starting")
        runner.start("scheduled")

    def scheduled_prune() -> None:
        removed = db.prune(store.current().schedule.retention_days)
        log.info("pruned old records: %s", removed)

    def sync_schedule() -> None:
        """Re-apply the cron trigger whenever the saved schedule changes."""
        cfg = store.current()
        sched = cfg.schedule
        current = (sched.scan_cron_hour, sched.scan_cron_minute, sched.scan_enabled)
        if current == (state["hour"], state["minute"], state["enabled"]):
            return
        state.update(hour=sched.scan_cron_hour, minute=sched.scan_cron_minute,
                     enabled=sched.scan_enabled)
        if scheduler.get_job("scan"):
            scheduler.remove_job("scan")
        if sched.scan_enabled:
            scheduler.add_job(scheduled_scan,
                              CronTrigger(hour=sched.scan_cron_hour, minute=sched.scan_cron_minute),
                              id="scan", replace_existing=True)
            log.info("nightly scan scheduled for %02d:%02d UTC",
                     sched.scan_cron_hour, sched.scan_cron_minute)
        else:
            log.info("automatic scans are disabled")

    scheduler.add_job(scheduled_prune, CronTrigger(hour=5, minute=0), id="prune",
                      replace_existing=True)
    scheduler.add_job(sync_schedule, CronTrigger(minute="*/5"), id="sync", replace_existing=True)
    sync_schedule()
    scheduler.start()
    return scheduler
