"""Leaderboards: who has solved the most *new* problems in the last 30 days.

A problem counts once per person, on the day of their first accepted submission, and only if that first
acceptance falls inside the window. Solving it again later, or having solved it before the window, adds nothing.
Everything is read from the submission store, so a leaderboard costs no judge calls.

"First accepted inside the window" is the same as "accepted inside the window, and never accepted before it",
so the query needs no per-submission flag: it looks only at submissions from the last 30 days and asks the
(user, platform, problem) index whether an older acceptance exists.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy import and_, exists, func
from sqlalchemy.orm import Session, aliased

from app import models
from app.platforms import registry

WINDOW_DAYS = 30
GLOBAL_LIMIT = 10


@dataclass
class Row:
    user: models.User
    solved: int
    rank: int = 0


def window_start(now: Optional[float] = None) -> int:
    return int((time.time() if now is None else now) - WINDOW_DAYS * 86400)


def new_solves(db: Session, user_ids: Iterable[int], since: Optional[int] = None) -> dict[int, set[tuple[str, str]]]:
    """{user id: {(platform, problem key)}} first solved since `since` (default: the last 30 days).

    Only users whose full history on that platform has been loaded are counted there: with part of the history
    missing, an old acceptance could hide and a problem solved years ago would look new."""
    ids = list(user_ids)
    if not ids:
        return {}
    since = window_start() if since is None else since
    S, Older = models.Submission, aliased(models.Submission)
    older_ac = exists().where(and_(
        Older.user_id == S.user_id, Older.platform == S.platform, Older.problem_key == S.problem_key,
        Older.accepted.is_(True), Older.submitted_at < since,
    ))
    loaded = {
        (u, p) for u, p in db.query(models.SubmissionSync.user_id, models.SubmissionSync.platform)
        .filter(models.SubmissionSync.user_id.in_(ids), models.SubmissionSync.full_sync_at.isnot(None))
    }
    rows = (
        db.query(S.user_id, S.platform, S.problem_key)
        .filter(S.user_id.in_(ids), S.accepted.is_(True), S.submitted_at >= since, ~older_ac)
        .group_by(S.user_id, S.platform, S.problem_key)
        .all()
    )
    out: dict[int, set[tuple[str, str]]] = {}
    for user_id, platform, problem_key in rows:
        if (user_id, platform) in loaded:
            out.setdefault(user_id, set()).add((platform, problem_key))
    return out


def _ranked(rows: list[Row]) -> list[Row]:
    """Most solved first, ties in name order; equal counts share a rank (1, 1, 3)."""
    rows.sort(key=lambda r: (-r.solved, r.user.username.lower()))
    for i, row in enumerate(rows):
        row.rank = rows[i - 1].rank if i and rows[i - 1].solved == row.solved else i + 1
    return rows


def group_problems(db: Session, group_id: int) -> set[tuple[str, str]]:
    """Every (platform, problem key) in the group's assignments: problem items and the problems of contest items."""
    items = db.query(models.AssignmentItem).join(models.Assignment).filter(models.Assignment.group_id == group_id).all()
    keys: set[tuple[str, str]] = set()
    for item in items:
        platform = registry.for_item(item)
        keys.update((item.platform, k) for k in platform.problem_keys(item))
    return keys


def for_group(db: Session, group: models.Group) -> list[Row]:
    """Members ranked by new problems solved from the group's assignments in the last 30 days."""
    members = [m.user for m in group.memberships]
    wanted = group_problems(db, group.id)
    solved = new_solves(db, [u.id for u in members])
    return _ranked([Row(u, len(solved.get(u.id, set()) & wanted)) for u in members])


def for_platform(db: Session, limit: int = GLOBAL_LIMIT) -> list[Row]:
    """The most hardworking accounts (students and the admin aside): new problems solved in the last 30 days,
    on any judge, whether or not they are in an assignment. Accounts with none are left out."""
    users = db.query(models.User).filter(models.User.user_type == "user").all()
    by_id = {u.id: u for u in users}
    solved = new_solves(db, by_id)
    rows = [Row(by_id[uid], len(s)) for uid, s in solved.items() if s]
    return _ranked(rows)[:limit]
