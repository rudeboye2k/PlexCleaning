"""Optional background scan schedule. Scans only — actions are never automatic."""
from __future__ import annotations

import logging

from .config import Config
from .db import Database

log = logging.getLogger(__name__)


def start_scheduler(cfg: Config, db: Database, runner) -> object | None:
    if not cfg.schedule.scan_enabled:
        return None
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        log.warning("apscheduler is not installed — scheduled scans are disabled")
        return None

    scheduler = BackgroundScheduler(timezone="UTC")

    def scheduled_scan() -> None:
        log.info("scheduled scan starting")
        runner.start("scheduled")

    def scheduled_prune() -> None:
        removed = db.prune(cfg.schedule.retention_days)
        log.info("pruned old records: %s", removed)

    scheduler.add_job(
        scheduled_scan,
        CronTrigger(hour=cfg.schedule.scan_cron_hour, minute=cfg.schedule.scan_cron_minute),
        id="scan", replace_existing=True,
    )
    scheduler.add_job(scheduled_prune, CronTrigger(hour=5, minute=0), id="prune", replace_existing=True)
    scheduler.start()
    log.info("scheduled nightly scan at %02d:%02d UTC",
             cfg.schedule.scan_cron_hour, cfg.schedule.scan_cron_minute)
    return scheduler
