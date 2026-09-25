"""Periodic auto-sync scheduler using APScheduler."""
import os
import logging
from datetime import datetime, timedelta

from sqlalchemy import or_

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.database import SessionLocal
from app.models import AssignmentItem, User
from app.sync import sync_item
from app import audit, histories, submissions
from app import recommend as rec
from app.platforms import registry

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

def _interval_hours() -> float:
    try:
        return float(os.getenv("SYNC_INTERVAL_HOURS") or 2)
    except ValueError:
        return 2.0


SYNC_INTERVAL_HOURS = _interval_hours()
# A copy refreshed less than the interval ago (minus a margin, so a run isn't skipped for being a minute short)
# counts as current.
_FRESH_FOR = timedelta(hours=SYNC_INTERVAL_HOURS) - timedelta(minutes=5)


async def _refresh_all_users() -> None:
    """Bring every user's stored submissions up to date, so histories stay current even for users whose
    contests and problems aren't in any assignment."""
    db = SessionLocal()
    try:
        users = db.query(User).all()
        for key in histories.HISTORY_PLATFORMS:
            await submissions.refresh_users(db, users, registry.get(key), max_age=_FRESH_FOR)
    except Exception:
        logger.warning("Refreshing all users' submissions failed", exc_info=True)
    finally:
        db.close()


async def _auto_sync_all() -> None:
    await _refresh_all_users()

    cutoff = datetime.utcnow() - _FRESH_FOR
    db = SessionLocal()
    try:
        items = (
            db.query(AssignmentItem)
            .filter(
                # "error" items are retried too: most errors are a judge that was briefly unreachable
                AssignmentItem.sync_status.in_(("done", "error")),
                or_(AssignmentItem.last_synced_at.is_(None), AssignmentItem.last_synced_at < cutoff),
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
            await sync_item(item_id, db, max_age=_FRESH_FOR)
        except Exception as exc:
            logger.warning("Auto-sync failed for item %d: %s", item_id, exc)
        finally:
            db.close()


async def _prune_audit_log() -> None:
    db = SessionLocal()
    try:
        removed = audit.prune(db)
        if removed:
            logger.info("Audit log: removed %d event(s) older than %d days", removed, audit.RETENTION_DAYS)
    except Exception:
        logger.warning("Pruning the audit log failed", exc_info=True)
    finally:
        db.close()


async def _retry_not_started() -> None:
    """Re-sync assignment items that are waiting for their contest to start."""
    cutoff = datetime.utcnow() - timedelta(minutes=30)
    db = SessionLocal()
    try:
        items = (
            db.query(AssignmentItem)
            .filter(
                AssignmentItem.sync_status == "not_started",
                AssignmentItem.last_synced_at < cutoff,
            )
            .all()
        )
        item_ids = [item.id for item in items]
    finally:
        db.close()

    if not item_ids:
        return

    logger.info("Retrying %d not-started contest(s)", len(item_ids))
    for item_id in item_ids:
        db = SessionLocal()
        try:
            await sync_item(item_id, db)
        except Exception as exc:
            logger.warning("Retry not-started item %d: %s", item_id, exc)
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
        await rec.prefetch_contest_problems(db, batch_size=1)
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
        # First run shortly after startup, so a redeploy catches up on anything that went stale meanwhile
        # (copies refreshed within the interval are skipped). Never next_run_time=None: in APScheduler that adds
        # the job *paused*, and it would never run.
        next_run_time=_dt.now() + timedelta(minutes=2),
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
    # NOTE: do NOT pass next_run_time=None — in APScheduler that pauses the job
    # forever. Omitting it schedules the first run at startup + 1 minute, by
    # which time the metadata job (which runs immediately) will have populated rows.
    _scheduler.add_job(
        _prefetch_contest_problems,
        "interval",
        minutes=1,
    )

    # Keep the audit log to its retention period (it holds IP addresses).
    _scheduler.add_job(_prune_audit_log, "interval", hours=24, next_run_time=_dt.now() + timedelta(minutes=10))

    # Re-check not-started contests every 30 minutes.
    _scheduler.add_job(
        _retry_not_started,
        "interval",
        minutes=30,
    )

    _scheduler.start()
    logger.info("Auto-sync scheduler started (interval: %gh)", SYNC_INTERVAL_HOURS)


def stop() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
