"""The local copy of every user's judge submissions.

Contests and problems are never looked up on the judges directly: a user's submissions are mirrored into the
`submissions` table once, refreshed incrementally afterwards, and every status is then read from the database.

    refresh_user()   bring one user's copy on one platform up to date (the only code that talks to a judge)
    for_contest()    a user's stored submissions in one contest, oldest first
    for_problem()    a user's stored submissions on one problem, oldest first

Refreshing only fetches what the copy is missing: everything newer than the newest stored submission, plus
anything the judge had not finished grading last time (its verdict may have changed). `SubmissionSync` records
when each refresh happened and how far the copy reaches.
"""
from __future__ import annotations

import asyncio
import logging
import weakref
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.platforms.base import Platform
from app.platforms.base import KnownState, SubmissionData

logger = logging.getLogger(__name__)

# A copy refreshed more recently than this is used as it is. Syncing a whole assignment touches the same users
# once per item; this keeps that to one refresh per user.
REFRESH_MAX_AGE = timedelta(seconds=90)

_CHUNK = 400  # ids per IN (...) query; SQLite caps bound parameters

_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[int, str], asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)


def _lock(user_id: int, platform_key: str) -> asyncio.Lock:
    """One lock per (user, platform) per event loop, so two syncs can't refresh the same copy at once."""
    per_loop = _locks.setdefault(asyncio.get_running_loop(), {})
    return per_loop.setdefault((user_id, platform_key), asyncio.Lock())


# ── reading ──────────────────────────────────────────────────────────────────

def sync_state(db: Session, user_id: int, platform_key: str) -> Optional[models.SubmissionSync]:
    return (
        db.query(models.SubmissionSync)
        .populate_existing()
        .filter_by(user_id=user_id, platform=platform_key)
        .first()
    )


def is_synced(db: Session, user_id: int, platform_key: str) -> bool:
    """True once a refresh has completed, i.e. the copy can answer "not solved" and not only "solved"."""
    state = sync_state(db, user_id, platform_key)
    return bool(state and state.last_synced_at)


def for_contest(db: Session, user_id: int, platform_key: str, contest_key: str) -> list[models.Submission]:
    return (
        db.query(models.Submission)
        .filter_by(user_id=user_id, platform=platform_key, contest_key=str(contest_key))
        .order_by(models.Submission.submitted_at, models.Submission.submission_id)
        .all()
    )


def for_problem(db: Session, user_id: int, platform_key: str, problem_key: str) -> list[models.Submission]:
    return (
        db.query(models.Submission)
        .filter_by(user_id=user_id, platform=platform_key, problem_key=problem_key)
        .order_by(models.Submission.submitted_at, models.Submission.submission_id)
        .all()
    )


def for_problems(db: Session, user_id: int, platform_key: str, problem_keys: Iterable[str]) -> list[models.Submission]:
    """A user's stored submissions on any of several problems, oldest first."""
    keys = list(problem_keys)
    out: list[models.Submission] = []
    for i in range(0, len(keys), _CHUNK):
        out.extend(
            db.query(models.Submission)
            .filter(
                models.Submission.user_id == user_id,
                models.Submission.platform == platform_key,
                models.Submission.problem_key.in_(keys[i:i + _CHUNK]),
            )
            .all()
        )
    out.sort(key=lambda s: (s.submitted_at, s.submission_id))
    return out


def rating_entry(db: Session, user_id: int, platform_key: str, contest_key: str) -> Optional[models.RatingEntry]:
    return (
        db.query(models.RatingEntry)
        .filter_by(user_id=user_id, platform=platform_key, contest_key=str(contest_key))
        .first()
    )


def daily_counts(db: Session, user_id: int, platform_key: str, since_epoch: float) -> dict[str, int]:
    """Submissions per UTC day ("YYYY-MM-DD") from `since_epoch` on."""
    counts: dict[str, int] = {}
    rows = (
        db.query(models.Submission.submitted_at)
        .filter(
            models.Submission.user_id == user_id,
            models.Submission.platform == platform_key,
            models.Submission.submitted_at >= since_epoch,
        )
        .all()
    )
    for (ts,) in rows:
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        counts[day] = counts.get(day, 0) + 1
    return counts


# ── refreshing ───────────────────────────────────────────────────────────────

def _known_state(db: Session, user_id: int, platform_key: str) -> KnownState:
    Sub = models.Submission
    base = (Sub.user_id == user_id, Sub.platform == platform_key)
    newest_id, newest_at = db.query(func.max(Sub.submission_id), func.max(Sub.submitted_at)).filter(*base).one()
    if newest_id is None:
        return KnownState()
    pending_id, pending_at = (
        db.query(func.min(Sub.submission_id), func.min(Sub.submitted_at))
        .filter(*base, Sub.final == False)  # noqa: E712 (SQL comparison)
        .one()
    )
    if pending_id is not None:
        return KnownState(stop_id=min(newest_id, pending_id), stop_at=min(newest_at, pending_at))
    return KnownState(stop_id=newest_id, stop_at=newest_at)


def purge(db: Session, user_id: int, platform_key: str) -> None:
    """Drop a user's stored copy for one platform (their handle changed, so the rows aren't theirs any more)."""
    db.query(models.Submission).filter_by(user_id=user_id, platform=platform_key).delete()
    db.query(models.RatingEntry).filter_by(user_id=user_id, platform=platform_key).delete()


def delete_user_data(db: Session, user_id: int) -> None:
    """Remove everything stored for a user (submissions, sync state, rating history), on every platform."""
    for model in (models.Submission, models.SubmissionSync, models.RatingEntry):
        db.query(model).filter_by(user_id=user_id).delete()


_UPDATABLE = ("verdict", "accepted", "final", "score", "max_score", "problem_name", "problem_rating")


def _store(db: Session, user_id: int, platform_key: str, rows: Iterable[SubmissionData]) -> tuple[int, int]:
    """Insert new submissions and refresh changed ones. Returns (added, updated)."""
    by_id: dict[int, SubmissionData] = {}
    for r in rows:
        by_id[r.submission_id] = r  # a repeated id (page overlap) keeps the last copy
    if not by_id:
        return 0, 0

    Sub = models.Submission
    existing: dict[int, models.Submission] = {}
    ids = list(by_id)
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i:i + _CHUNK]
        for sub in db.query(Sub).filter(Sub.user_id == user_id, Sub.platform == platform_key,
                                        Sub.submission_id.in_(chunk)):
            existing[sub.submission_id] = sub

    inserts, updates = [], []
    for sid, data in by_id.items():
        current = existing.get(sid)
        if current is None:
            inserts.append({**asdict(data), "user_id": user_id, "platform": platform_key})
            continue
        changed = {f: getattr(data, f) for f in _UPDATABLE if getattr(current, f) != getattr(data, f)}
        if changed:
            updates.append({"id": current.id, **changed})
    if inserts:
        db.bulk_insert_mappings(Sub, inserts)
    if updates:
        db.bulk_update_mappings(Sub, updates)
    return len(inserts), len(updates)


def _replace_ratings(db: Session, user_id: int, platform_key: str, history) -> None:
    """Make the stored rating history equal to `history`, updating rows in place."""
    current = {
        e.contest_key: e
        for e in db.query(models.RatingEntry).filter_by(user_id=user_id, platform=platform_key)
    }
    seen: set[str] = set()
    for h in history:
        if h.contest_key in seen:
            continue
        seen.add(h.contest_key)
        entry = current.get(h.contest_key)
        if entry is None:
            entry = models.RatingEntry(user_id=user_id, platform=platform_key, contest_key=h.contest_key)
            db.add(entry)
        entry.contest_name = h.contest_name
        entry.rank = h.rank
        entry.old_rating = h.old_rating
        entry.new_rating = h.new_rating
        entry.performance = h.performance
        entry.rated_at = h.rated_at
    for key, entry in current.items():
        if key not in seen:
            db.delete(entry)


async def refresh_user(
    db: Session,
    user,
    platform: Platform,
    *,
    max_age: timedelta = REFRESH_MAX_AGE,
    force: bool = False,
) -> Optional[models.SubmissionSync]:
    """Bring `user`'s stored submissions on `platform` up to date. No-op (returns None) if the user has no
    handle there or the platform has no submissions to fetch. Raises if the judge can't be reached; the
    error is also recorded on the sync state and the stored copy is left as it was."""
    handle = platform.handle_of(user)
    if not handle or not platform.has_submissions:
        return None

    async with _lock(user.id, platform.key):
        state = sync_state(db, user.id, platform.key)
        if state is None:
            state = models.SubmissionSync(user_id=user.id, platform=platform.key, submission_count=0)
            db.add(state)
        elif state.handle and state.handle.lower() != handle.lower():
            logger.info("%s handle of user %s changed (%s -> %s): resetting stored submissions",
                        platform.key, user.id, state.handle, handle)
            purge(db, user.id, platform.key)
            state.last_synced_at = state.full_sync_at = None
            state.newest_submission_id = state.newest_submitted_at = None
            state.submission_count = 0
            state.last_error = None

        now = datetime.utcnow()
        same_handle = bool(state.handle) and state.handle.lower() == handle.lower()
        if not force and same_handle and state.last_synced_at and now - state.last_synced_at < max_age:
            db.commit()
            return state

        state.handle = handle
        state.last_attempt_at = now
        db.commit()  # persist the reset / the new state row before going to the network

        known = _known_state(db, user.id, platform.key)
        try:
            rows = await platform.fetch_submissions(handle, known)
        except Exception as exc:
            db.rollback()
            state = sync_state(db, user.id, platform.key)
            state.last_error = str(exc)[:500]
            db.commit()
            raise

        added, updated = _store(db, user.id, platform.key, rows)
        db.flush()

        Sub = models.Submission
        newest_id, newest_at, count = (
            db.query(func.max(Sub.submission_id), func.max(Sub.submitted_at), func.count(Sub.id))
            .filter(Sub.user_id == user.id, Sub.platform == platform.key)
            .one()
        )
        state.newest_submission_id = newest_id
        state.newest_submitted_at = newest_at
        state.submission_count = count
        state.last_synced_at = datetime.utcnow()
        state.last_error = None
        if known.stop_id is None and state.full_sync_at is None:
            state.full_sync_at = state.last_synced_at

        try:
            history = await platform.fetch_rating_history(handle)
            if history is not None:
                _replace_ratings(db, user.id, platform.key, history)
        except Exception:
            logger.warning("Could not refresh %s rating history for %s", platform.key, handle, exc_info=True)

        db.commit()
        logger.info("%s submissions of %s: +%d new, %d changed, %d stored", platform.key, handle, added, updated, count)
        return state


async def refresh_users(db: Session, users: Iterable, platform: Platform, **kwargs) -> dict[int, str]:
    """Refresh several users in turn. Returns {user_id: error} for the ones that failed; the rest are current."""
    errors: dict[int, str] = {}
    for user in users:
        try:
            await refresh_user(db, user, platform, **kwargs)
        except Exception as exc:
            logger.warning("Refreshing %s submissions of user %s failed: %s", platform.key, user.id, exc)
            errors[user.id] = str(exc) or exc.__class__.__name__
    return errors
