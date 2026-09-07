"""Periodic auto-sync scheduler using APScheduler."""
import os
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.database import SessionLocal
from app.models import AssignmentItem
from app.sync import sync_item

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


def start() -> None:
    global _scheduler
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _auto_sync_all,
        "interval",
        hours=SYNC_INTERVAL_HOURS,
        next_run_time=None,
    )
    _scheduler.start()
    logger.info("Auto-sync scheduler started (interval: %dh)", SYNC_INTERVAL_HOURS)


def stop() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
