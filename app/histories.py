"""Keeping users' stored submission histories in step with their handles.

When a handle is saved (registration, profile edit, admin edit) the history behind it is loaded straight away in
the background, so the first item that needs it doesn't have to wait, and a wrong handle shows up as an error on the
profile page instead of surfacing later on some assignment. Clearing a handle drops the stored copy.
"""
import logging
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app import models, submissions
from app.database import SessionLocal
from app.platforms import registry

logger = logging.getLogger(__name__)

# Platforms that have submissions to load, in display order
HISTORY_PLATFORMS = ("codeforces", "atcoder", "kilonova")


def handles_of(user) -> dict[str, Optional[str]]:
    """The user's handle on each platform (take this before editing, then pass it to apply_handle_changes)."""
    return {key: registry.get(key).handle_of(user) for key in HISTORY_PLATFORMS}


def _same(a: Optional[str], b: Optional[str]) -> bool:
    return (a or "").lower() == (b or "").lower()


def apply_handle_changes(db: Session, user, before: Optional[dict[str, Optional[str]]] = None) -> list[str]:
    """Reconcile stored histories with the user's current handles and return the platforms whose history has to be
    (re)loaded. `before` is the result of handles_of() taken prior to the edit; None means a new user.
    A cleared handle drops that platform's stored rows. The caller commits."""
    to_load: list[str] = []
    for key in HISTORY_PLATFORMS:
        now = registry.get(key).handle_of(user)
        old = (before or {}).get(key)
        if before is not None and _same(old, now):
            continue
        if now:
            to_load.append(key)  # refresh_user drops rows that belonged to the old handle
        else:
            submissions.purge(db, user.id, key)
            db.query(models.SubmissionSync).filter_by(user_id=user.id, platform=key).delete()
    return to_load


async def load_histories(user_id: int, platform_keys: Iterable[str], *, force: bool = False) -> None:
    """Background task: refresh the given platforms' histories for one user. Failures are recorded on the
    sync state (shown on the profile page), never raised."""
    keys = list(platform_keys)
    if not keys:
        return
    db = SessionLocal()
    try:
        user = db.get(models.User, user_id)
        if not user:
            return
        for key in keys:
            await submissions.refresh_users(db, [user], registry.get(key), force=force)
    except Exception:
        logger.warning("Loading histories for user %s failed", user_id, exc_info=True)
    finally:
        db.close()


def recent_submissions(db: Session, user, limit: int = 20) -> list[dict]:
    """The user's newest submissions on any platform, ready for display (times are UTC)."""
    rows = []
    for sub in submissions.recent(db, user.id, limit):
        platform = registry.get(sub.platform)
        verdict = sub.verdict or "…"
        if sub.verdict == "PT" and sub.score is not None and sub.max_score:
            verdict = f"PT {sub.score:g}/{sub.max_score:g}"
        rows.append({
            "at": datetime.fromtimestamp(sub.submitted_at, timezone.utc),
            "platform": platform,
            "problem": platform.submission_problem_label(sub),
            "problem_url": platform.submission_problem_url(sub),
            "url": platform.submission_url(sub),
            "verdict": verdict,
            "accepted": sub.accepted,
            "pending": not sub.final,
            "mode": {"VIRTUAL": "virtual", "CONTESTANT": "live"}.get(sub.participant_type or ""),
            "team": sub.team_name,
        })
    return rows


def history_status(db: Session, user) -> list[dict]:
    """One entry per platform the user has a handle on, for the profile page."""
    rows = []
    for key in HISTORY_PLATFORMS:
        platform = registry.get(key)
        handle = platform.handle_of(user)
        if not handle:
            continue
        state = submissions.sync_state(db, user.id, key)
        rows.append({
            "label": platform.label,
            "handle": handle,
            "count": state.submission_count if state else 0,
            "synced_at": state.last_synced_at if state else None,
            "error": state.last_error if state else None,
        })
    return rows
