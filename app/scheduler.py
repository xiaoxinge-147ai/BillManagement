"""每天自动保存快照（时间可配，支持精确到秒）。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db import SessionLocal
from app.snapshot import save_all_sites

logger = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


def _build_trigger(expr: str) -> CronTrigger:
    tz = settings.timezone
    parts = (expr or "").split()
    try:
        if len(parts) == 6:
            s, mi, h, d, mo, dow = parts
            return CronTrigger(second=s, minute=mi, hour=h, day=d, month=mo, day_of_week=dow, timezone=tz)
        if len(parts) == 5:
            return CronTrigger.from_crontab(expr, timezone=tz)
    except Exception:  # noqa: BLE001
        logger.warning("save_cron 解析失败(%s)，用默认 02:00", expr)
    return CronTrigger(hour=2, minute=0, timezone=tz)


def _job() -> None:
    yesterday = (datetime.now(ZoneInfo(settings.timezone)) - timedelta(days=1)).date()
    logger.info("自动保存快照 stat_date=%s", yesterday)
    db = SessionLocal()
    try:
        save_all_sites(db, yesterday)
        # 保存后后台读取各总站上游用量
        from sqlalchemy import select
        from app.models import Site
        from app.recon import fetch_all_background
        for s in db.scalars(select(Site).where(Site.site_type == "master")).all():
            fetch_all_background(s.id, yesterday.isoformat())
    finally:
        db.close()


JOB_ID = "daily_save"


def _apply(enabled: bool, cron: str) -> None:
    """按配置增删定时任务（作用于运行中的调度器）。"""
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(JOB_ID)
    except Exception:  # noqa: BLE001
        pass
    if enabled:
        _scheduler.add_job(_job, _build_trigger(cron), id=JOB_ID, replace_existing=True)
    logger.info("定时任务配置: enabled=%s cron=%s tz=%s", enabled, cron, settings.timezone)


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone=settings.timezone)
    _scheduler.start()
    db = SessionLocal()
    try:
        from app.appconfig import get_schedule
        enabled, cron = get_schedule(db)
    finally:
        db.close()
    _apply(enabled, cron)


def reschedule(enabled: bool, cron: str) -> None:
    """配置变更后即时生效（无需重启进程）。"""
    if _scheduler is None:
        start_scheduler()
    _apply(enabled, cron)


def next_run_time():
    if _scheduler is None:
        return None
    job = _scheduler.get_job(JOB_ID)
    return job.next_run_time if job else None


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
