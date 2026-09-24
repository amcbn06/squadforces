"""Syncing an assignment item.

An item's results are derived from the local submission store (app/submissions.py), never fetched per item:

    1. refresh the stored submissions of the members who have a handle on the item's platform
       (incremental, and skipped if the copy is only seconds old)
    2. hand the item to its platform module (app/platforms/<platform>.py), which fills in titles / problem
       lists and turns the stored submissions into Result / ProblemResult rows

`AssignmentItem.sync_status` tracks `pending -> syncing -> done | error | not_started`.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app import models, submissions
from app.platforms import registry
from app.scraper import codeforces as cf

logger = logging.getLogger(__name__)

SYNC_TIMEOUT_SECONDS = 300  # 5 minutes hard cap per item


class PartialSyncError(Exception):
    """Some members' submissions couldn't be refreshed. Results were still derived from what is stored."""


async def sync_item(item_id: int, db: Session, *, max_age: Optional[timedelta] = None) -> None:
    """`max_age`: how old a member's stored submissions may be before they are refreshed (default: seconds)."""
    item = db.get(models.AssignmentItem, item_id)
    if not item:
        return

    item.sync_status = "syncing"
    item.sync_error = None
    db.commit()

    try:
        await asyncio.wait_for(_sync(item, db, max_age), timeout=SYNC_TIMEOUT_SECONDS)
        item.last_synced_at = datetime.utcnow()
        item.sync_status = "done"
        item.sync_error = None
    except asyncio.TimeoutError:
        item.sync_status = "error"
        item.sync_error = f"Sync timed out after {SYNC_TIMEOUT_SECONDS}s"
    except cf.ContestNotStartedError:
        item.sync_status = "not_started"
        item.sync_error = "Contest has not started yet"
        item.last_synced_at = datetime.utcnow()
    except PartialSyncError as exc:
        item.last_synced_at = datetime.utcnow()  # results are current for everyone else
        item.sync_status = "error"
        item.sync_error = str(exc)
    except Exception as exc:
        logger.warning("Sync of item %s failed", item_id, exc_info=True)
        item.sync_status = "error"
        item.sync_error = str(exc) or exc.__class__.__name__

    db.commit()


async def _sync(item: models.AssignmentItem, db: Session, max_age: Optional[timedelta]) -> None:
    platform = registry.for_item(item)
    if platform.manual_status(item):
        return  # nothing to fetch: solved marks and titles are entered by hand

    group = db.get(models.Assignment, item.assignment_id).group
    members = [m.user for m in group.memberships]

    kwargs = {} if max_age is None else {"max_age": max_age}
    errors = await submissions.refresh_users(db, [u for u in members if platform.handle_of(u)], platform, **kwargs)
    await platform.sync_item(item, members, db)

    if errors:
        by_id = {u.id: u for u in members}
        failed = "; ".join(f"{platform.handle_of(by_id[uid])}: {msg}" for uid, msg in errors.items())
        raise PartialSyncError(f"Could not update {platform.label} submissions ({failed}). Showing stored data.")
