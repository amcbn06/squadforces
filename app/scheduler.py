"""Periodic auto-sync scheduler using APScheduler."""
import os
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.database import SessionLocal
from app.models import AssignmentItem
from app.sync import sync_item
from app import recommend as rec

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

SYNC_INTERVAL_HOURS = int(os.getenv("SYNC_INTERVAL_HOURS", "6"))


async def _auto_sync_all() -> None:
    cutoff = datetime.utcnow() - timedelta(hours=SYNC_INTERVAL_HOURS)
    db = SessionLocal()
    try:
        items = (
            db.query(AssignmentItem)
            .filter(
                AssignmentItem.sync_status == "done",
                AssignmentItem.last_synced_at < cutoff,
            )
            .all()
        )
        item_ids = [item.id for item in items]
    finally:
        db.close()

    if not item_ids:
        return

    logger.info("Auto-sync: %d item(s) due for refresh", len(item_ids))
    for item_id in item_ids:
        db = SessionLocal()
        try:
            await sync_item(item_id, db)
        except Exception as exc:
            logger.warning("Auto-sync failed for item %d: %s", item_id, exc)
        finally:
            db.close()


async def _refresh_contest_metadata() -> None:
    db = SessionLocal()
    try:
        await rec.refresh_contest_metadata(db)
    finally:
        db.close()


async def _prefetch_contest_problems() -> None:
    db = SessionLocal()
    try:
        await rec.prefetch_contest_problems(db, batch_size=20)
    finally:
        db.close()


def start() -> None:
    global _scheduler
    from datetime import datetime as _dt
    _scheduler = AsyncIOScheduler()

    _scheduler.add_job(
        _auto_sync_all,
        "interval",
        hours=SYNC_INTERVAL_HOURS,
        next_run_time=None,
    )

    # Refresh the list of recent CF contests once a day (1 API call).
    # next_run_time=now() means it runs immediately on startup so the DB
    # is populated even before the first /recommend visit.
    _scheduler.add_job(
        _refresh_contest_metadata,
        "interval",
        hours=24,
        next_run_time=_dt.now(),
    )

    # Fetch problem ratings for 1 uncached contest per minute.
    # 300 contests → ~5 h to fully warm the cache.
    # One call per run keeps pressure on the CF rate limiter minimal.
    _scheduler.add_job(
        _prefetch_contest_problems,
        "interval",
        minutes=1,
        next_run_time=None,  # wait for metadata job to populate rows first
    )

    _scheduler.start()
    logger.info("Auto-sync scheduler started (interval: %dh)", SYNC_INTERVAL_HOURS)


def stop() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
