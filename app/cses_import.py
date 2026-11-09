"""Counting CSES solves, which CSES itself will not tell us.

CSES has no API and shows a member's solved tasks only to that member, logged in. So the member's own browser reads
the list off their CSES page (the bookmarklet built from static/cses-export.js) and hands it to this app, which
stores it as submissions (platform "cses"): the group boards, the matrix and the rest then work as for any judge.
No CSES password or cookie ever reaches this app.

Nothing about the list itself can be verified, so the one public number CSES does show for anyone, the total count
of submissions on `cses.fi/user/<id>`, is used as a plausibility check:
  * an account can't have solved more tasks than it has submissions, and
  * between two imports it can't have gained more solved tasks than it gained submissions.
That ties a claim to real activity on a real account without trusting the browser.

The first import only records what the member had already solved: those rows are `backfilled` and count towards
nothing time-based (the 30-day leaderboard, the heatmap). Tasks that show up in later imports are stamped with the
import time and count like any new solve.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app import models
from app.platforms import cses as cses_platform

PLATFORM = "cses"
MAX_SOLVED = 1000        # CSES has well under 500 tasks; this only stops absurd input
COOLDOWN_SECONDS = 60    # between imports by one member, so the profile check can't be used to hammer cses.fi
PROFILE_URL = "https://cses.fi/user/{id}"


class ImportRejected(Exception):
    """The import can't be accepted. `message` is shown to the member as it is."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class PublicProfile:
    name: str
    submissions: int


@dataclass(frozen=True)
class ImportResult:
    cses_id: str
    name: str
    solved: int           # tasks in the list that was sent
    new: int              # of those, the ones not known before
    baseline: bool        # the first import (or a different account): recorded, but counts for nothing time-based
    submissions: int      # what cses.fi shows for the account


_NAME = re.compile(r"<h1>\s*User\s+(.*?)\s*</h1>", re.S)
_COUNT = re.compile(r"Submission count:\s*</td>\s*<td[^>]*>\s*(\d+)\s*<", re.S)


def parse_profile(page: str) -> Optional[PublicProfile]:
    """Name and submission count from a cses.fi/user/<id> page, or None if it isn't one."""
    name, count = _NAME.search(page), _COUNT.search(page)
    if not name or not count:
        return None
    return PublicProfile(html.unescape(name.group(1)), int(count.group(1)))


async def fetch_public_profile(cses_id: str) -> Optional[PublicProfile]:
    """The account's public page, or None if there is no such user. Raises ImportRejected if cses.fi can't be read:
    the check is the whole point, so an import is never accepted without it."""
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Squadforces CSES import"}) as client:
            response = await client.get(PROFILE_URL.format(id=cses_id))
    except httpx.HTTPError:
        raise ImportRejected("Couldn't reach cses.fi to check the account. Try again in a minute.", 503)
    if response.status_code == 404:
        return None
    profile = parse_profile(response.text) if response.status_code == 200 else None
    if profile is None:
        raise ImportRejected("cses.fi didn't answer the way I expected, so the account can't be checked. Try again later.", 503)
    return profile


def clean_task_ids(raw) -> set[str]:
    if not isinstance(raw, (list, tuple)):
        raise ImportRejected("The list of solved tasks is missing.")
    if len(raw) > MAX_SOLVED:
        raise ImportRejected(f"That list is too long (at most {MAX_SOLVED} tasks).")
    ids: set[str] = set()
    for entry in raw:
        text = str(entry).strip()
        if not re.fullmatch(r"\d{1,7}", text):
            raise ImportRejected(f"'{text[:20]}' is not a CSES task number.")
        ids.add(str(int(text)))
    return ids


async def import_solved(db: Session, user: models.User, cses_id, raw_solved, *, now: Optional[float] = None) -> ImportResult:
    """Record `raw_solved` (CSES task numbers) as `user`'s solves on the CSES account `cses_id`.

    Raises ImportRejected without changing anything but the attempt time (which is what the cooldown reads); the
    caller commits either way. On success the caller records the audit event and commits."""
    now = time.time() if now is None else now
    stamp = datetime.utcfromtimestamp(now)  # naive UTC, like every other stored time here

    cses_id = str(cses_id or "").strip()
    if not re.fullmatch(r"\d{1,9}", cses_id):
        raise ImportRejected("Enter your CSES user number: the number in cses.fi/user/<number>.")
    solved = clean_task_ids(raw_solved)
    if db.query(models.User).filter(models.User.cses_id == cses_id, models.User.id != user.id).first():
        raise ImportRejected("That CSES account is already linked to another member.", 409)

    state = db.query(models.SubmissionSync).filter_by(user_id=user.id, platform=PLATFORM).first()
    if state is None:
        state = models.SubmissionSync(user_id=user.id, platform=PLATFORM, submission_count=0)
        db.add(state)
    if state.last_attempt_at and (stamp - state.last_attempt_at).total_seconds() < COOLDOWN_SECONDS:
        raise ImportRejected("You imported a moment ago. Wait a minute and try again.", 429)
    state.last_attempt_at = stamp
    db.flush()

    profile = await fetch_public_profile(cses_id)
    if profile is None:
        raise ImportRejected("There is no CSES user with that number.", 404)

    baseline = state.handle != cses_id  # the first import, or a different account than the last one
    known: set[str] = set()
    if not baseline:
        known = {key for (key,) in db.query(models.Submission.problem_key).filter_by(user_id=user.id, platform=PLATFORM)}
    new = solved - known

    if len(solved) > profile.submissions:
        raise ImportRejected(
            f"{profile.name} has only {profile.submissions} submissions on CSES, so can't have {len(solved)} solved "
            "tasks. Check that the number is yours.")
    if not baseline:
        fresh = max(profile.submissions - state.submission_count, 0)
        if len(new) > fresh:
            raise ImportRejected(
                f"CSES shows {fresh} new submission{'s' if fresh != 1 else ''} since your last import, but this list "
                f"has {len(new)} newly solved task{'s' if len(new) != 1 else ''}. Make sure you are logged in to "
                "your own CSES account and the number above is yours.")

    if baseline:  # a different account's rows would be someone else's progress
        db.query(models.Submission).filter_by(user_id=user.id, platform=PLATFORM).delete()
        state.full_sync_at = None
    for task in sorted(new, key=int):
        db.add(models.Submission(
            user_id=user.id, platform=PLATFORM, submission_id=int(task), problem_key=task, verdict="AC",
            accepted=True, final=True, submitted_at=int(now), backfilled=baseline,
        ))
    state.handle = cses_id
    state.last_synced_at = stamp
    state.submission_count = profile.submissions
    state.full_sync_at = state.full_sync_at or stamp
    user.cses_id = cses_id
    db.flush()  # the results below read what was just added

    items = (
        db.query(models.AssignmentItem)
        .join(models.Assignment, models.Assignment.id == models.AssignmentItem.assignment_id)
        .join(models.GroupMembership, models.GroupMembership.group_id == models.Assignment.group_id)
        .filter(models.GroupMembership.user_id == user.id, models.AssignmentItem.platform == PLATFORM,
                models.AssignmentItem.type == "problem", models.AssignmentItem.external_id.in_(solved))
        .all()
    )
    for item in items:
        cses_platform.mark_solved_from_submissions(db, item, [user.id])

    return ImportResult(cses_id=cses_id, name=profile.name, solved=len(solved), new=len(new), baseline=baseline,
                        submissions=profile.submissions)
