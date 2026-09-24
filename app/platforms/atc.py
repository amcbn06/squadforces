"""AtCoder: contests and tasks. Data comes from AtCoder Problems (kenkoooo.com) and AtCoder's public rating
history; the HTTP calls are in app/scraper/atcoder.py."""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import ParseResult

from app import models, submissions
from app.platforms.base import KnownState, ParsedLink, Platform, RatingData, SubmissionData
from app.scraper import atcoder as api

logger = logging.getLogger(__name__)

ATC = "atc"

_TASK_PATH = re.compile(r"^/contests/([^/]+)/tasks/([^/]+)/?$")
_CONTEST_PATH = re.compile(r"^/contests/([^/]+)(?:/|$)")
_BARE_TASK = re.compile(r"^[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9]+$")
_PENDING = {"WJ", "WR", "Judging"}  # results that will still change
_CONTEST_SUFFIX = ".contest.atcoder.jp"


def is_url(url: ParseResult) -> bool:
    host = (url.hostname or "").lower()
    return host == "atcoder.jp" or host.endswith(".atcoder.jp")


def is_bare_id(token: str) -> bool:
    return bool(_BARE_TASK.match(token))


def _task_link(task_id: str) -> ParsedLink:
    return ParsedLink("atcoder", "problem", task_id, f"AtCoder problem {task_id}")


def _contest_link(contest_id: str) -> ParsedLink:
    return ParsedLink("atcoder", "contest", contest_id, f"AtCoder contest {contest_id}")


class AtCoder(Platform):
    key = "atcoder"
    label = "AtCoder"
    icon = "ac.png"
    handle_attr = "atcoder_handle"
    profile_url = "https://atcoder.jp/users/{handle}"
    supports_contests = True
    has_submissions = True

    # ── recognition ──────────────────────────────────────────────────────────

    def parse_url(self, source: str, url: ParseResult) -> Optional[ParsedLink]:
        m = _TASK_PATH.match(url.path)
        if m:
            return _task_link(m.group(2))
        m = _CONTEST_PATH.match(url.path)
        if m and m.group(1) != "archive":
            return _contest_link(m.group(1))
        return None

    def parse_bare(self, token: str) -> Optional[ParsedLink]:
        return _task_link(token) if _BARE_TASK.match(token) else None

    def parse_form(self, item_type: str, raw: str) -> tuple[Optional[ParsedLink], Optional[str]]:
        if item_type == "contest":
            contest_id = api.parse_contest_id(raw)
            if not contest_id:
                return None, "Invalid AtCoder contest URL or slug."
            return _contest_link(contest_id), None
        parsed = api.parse_problem_id(raw)
        if not parsed:
            return None, "Invalid AtCoder problem URL (e.g. https://atcoder.jp/contests/abc123/tasks/abc123_a)."
        return _task_link(parsed[1]), None

    # ── presentation ─────────────────────────────────────────────────────────

    def item_url(self, item) -> str:
        if item.type == "contest":
            return f"https://atcoder.jp/contests/{item.external_id}"
        home = item.external_id.rsplit("_", 1)[0]
        return f"https://atcoder.jp/contests/{home}/tasks/{item.external_id}"

    def problem_url(self, item, contest_problem) -> str:
        return f"https://atcoder.jp/contests/{item.external_id}/tasks/{contest_problem.platform_problem_id}"

    def default_title(self, item) -> str:
        return item.external_id

    # ── fetching ─────────────────────────────────────────────────────────────

    async def fetch_submissions(self, handle: str, known: KnownState) -> list[SubmissionData]:
        raw = await api.get_user_submissions(handle, known.stop_at or 0)
        return [
            SubmissionData(
                submission_id=s["id"],
                problem_key=s["problem_id"],
                submitted_at=s.get("epoch_second", 0),
                verdict=s.get("result", "") or "",
                accepted=s.get("result") == "AC",
                final=s.get("result") not in _PENDING,
                contest_key=s.get("contest_id"),
                score=s.get("point"),
                language=s.get("language"),
            )
            for s in raw
            if "id" in s and s.get("problem_id")
        ]

    async def fetch_rating_history(self, handle: str) -> list[RatingData]:
        out = []
        for h in await api.get_rating_history(handle):
            screen = h.get("ContestScreenName") or ""
            contest_key = screen[: -len(_CONTEST_SUFFIX)] if screen.endswith(_CONTEST_SUFFIX) else screen
            if not contest_key:
                continue
            rated = bool(h.get("IsRated"))
            end = h.get("EndTime")
            try:
                rated_at = int(datetime.fromisoformat(end).timestamp()) if end else None
            except ValueError:
                rated_at = None
            out.append(RatingData(
                contest_key=contest_key,
                contest_name=h.get("ContestName") or None,
                rank=h.get("Place"),
                old_rating=h.get("OldRating") if rated else None,
                new_rating=h.get("NewRating") if rated else None,
                performance=h.get("Performance") if rated else None,
                rated_at=rated_at,
            ))
        return out

    # ── item sync ────────────────────────────────────────────────────────────

    async def sync_item(self, item, members: list, db) -> None:
        handled = [u for u in members if u.atcoder_handle]
        if item.type == "contest":
            await _sync_contest(item, handled, db)
        else:
            await _sync_problem(item, handled, db)


# ── classification ───────────────────────────────────────────────────────────

def classify_contest(
    subs,
    problem_ids: set[str],
    contest_start: int,
    contest_end: int,
    had_rated: bool,
) -> tuple[dict[str, dict], bool]:
    """Classify one user's submissions for one contest by comparing submission times to the contest window.

    `subs` are stored submissions (problem_key, verdict, submitted_at, submission_id). Returns:
        per_problem: {problem_id: {solved, solve_type, attempts, wrong_verdicts}}
        did_participate: the user submitted during the contest window or was rated in it
    """
    relevant = sorted(
        [s for s in subs if s.problem_key in problem_ids],
        key=lambda s: (s.submitted_at, s.submission_id),
    )

    did_live = any(contest_start <= s.submitted_at < contest_end for s in relevant)
    did_participate = did_live or had_rated

    by_problem: dict[str, list] = defaultdict(list)
    for s in relevant:
        by_problem[s.problem_key].append(s)

    per_problem: dict[str, dict] = {}
    for pid, psubs in by_problem.items():
        live_subs = [s for s in psubs if contest_start <= s.submitted_at < contest_end]
        post_subs = [s for s in psubs if s.submitted_at >= contest_end]

        live_ac = next((s for s in live_subs if s.verdict == "AC"), None)
        post_ac = next((s for s in post_subs if s.verdict == "AC"), None)
        # Tasks reused by a later contest (an ADT) were often solved long before it started.
        pre_ac = next((s for s in psubs if s.submitted_at < contest_start and s.verdict == "AC"), None)

        if live_ac:
            wrong_before = [
                s.verdict for s in live_subs
                if s.submitted_at < live_ac.submitted_at and s.verdict not in ("AC", None, "")
            ]
            per_problem[pid] = {
                "solved": True,
                "solve_type": "live",
                "attempts": len(wrong_before),
                "wrong_verdicts": list(dict.fromkeys(wrong_before)),
            }
        elif post_ac:
            all_wrong_before = [
                s.verdict for s in psubs
                if s.submitted_at < post_ac.submitted_at and s.verdict not in ("AC", None, "")
            ]
            per_problem[pid] = {
                "solved": True,
                "solve_type": "upsolving" if did_participate else "standalone",
                "attempts": len(all_wrong_before),
                "wrong_verdicts": list(dict.fromkeys(all_wrong_before)),
            }
        elif pre_ac:
            per_problem[pid] = {"solved": True, "solve_type": "standalone", "attempts": 0, "wrong_verdicts": []}
        else:
            all_wrong = [s.verdict for s in psubs if s.verdict not in ("AC", None, "")]
            per_problem[pid] = {
                "solved": False,
                "solve_type": None,
                "attempts": 0,
                "wrong_verdicts": list(dict.fromkeys(all_wrong)),
            }

    return per_problem, did_participate


# ── contest sync ─────────────────────────────────────────────────────────────

_CONTEST_NAMES = {"abc": "AtCoder Beginner Contest", "arc": "AtCoder Regular Contest", "agc": "AtCoder Grand Contest"}


def task_index(task: dict, contest_id: str) -> str:
    """Row label for a task. A task from its own contest keeps the letter in its ID (abc300_h -> "H", which also
    sorts after G, unlike AtCoder's "Ex"). A task reused from another contest has no useful suffix, so it gets
    the label AtCoder gives it in this contest."""
    task_id = task.get("id", "")
    home, sep, suffix = task_id.rpartition("_")
    if not sep:
        return task_id[-1].upper()
    if home != contest_id and task.get("contest_index"):
        return str(task["contest_index"]).upper()
    return suffix.upper()


async def _sync_contest(item, members: list, db) -> None:
    tasks = await api.get_contest_tasks(item.external_id)
    if not item.title:
        m = re.match(r"^(abc|arc|agc)(\d+)$", item.external_id.lower())
        if m:
            item.title = f"{_CONTEST_NAMES[m.group(1)]} {int(m.group(2))}"
        else:
            item.title = item.external_id.upper()

    # Keyed by problem id, not by letter: a contest made of reused tasks (an ADT) can hold several tasks whose
    # IDs end in the same letter, and a row must keep its own hints/results if its label ever changes.
    existing = {cp.platform_problem_id: cp for cp in item.contest_problems}
    for task in tasks:
        task_id = task.get("id", "")
        idx = task_index(task, item.external_id)
        raw_title = task.get("title", task_id)
        name = re.sub(r"^[A-Za-z]\.\s*", "", raw_title) or raw_title  # kenkoooo titles carry a leading "X. "
        cp = existing.get(task_id)
        if cp is None:
            cp = models.ContestProblem(
                assignment_item_id=item.id,
                platform_problem_id=task_id,
                index=idx,
                name=name,
                rating=task.get("difficulty"),
            )
            db.add(cp)
            db.flush()
            existing[task_id] = cp
        else:
            cp.index = idx
            cp.name = name
            if task.get("difficulty"):
                cp.rating = int(round(task["difficulty"]))

    # Contest timing tells live solves from upsolves
    timing = await api.get_contest_timing(item.external_id)
    if timing and timing["start_epoch_second"] > 0:
        contest_start = timing["start_epoch_second"]
        contest_end = contest_start + timing["duration_second"]
    else:
        contest_start = contest_end = 0
    has_timing = contest_start > 0

    problem_ids = {cp.platform_problem_id for cp in existing.values()}

    for user in members:
        if not submissions.is_synced(db, user.id, "atcoder"):
            continue
        subs = submissions.for_problems(db, user.id, "atcoder", problem_ids)
        entry = submissions.rating_entry(db, user.id, "atcoder", item.external_id)
        had_rated = entry is not None

        if has_timing:
            classification, _ = classify_contest(subs, problem_ids, contest_start, contest_end, had_rated)
        else:
            # No timing available — mark solved/not-solved without type info
            solved_set = {s.problem_key for s in subs if s.verdict == "AC"}
            classification = {
                pid: {"solved": pid in solved_set, "solve_type": None, "attempts": 0, "wrong_verdicts": []}
                for pid in problem_ids
            }

        result = db.query(models.Result).filter_by(assignment_item_id=item.id, user_id=user.id).first()
        if not result:
            result = models.Result(assignment_item_id=item.id, user_id=user.id)
            db.add(result)

        result.problems_solved_count = sum(1 for info in classification.values() if info.get("solved"))
        result.problems_total_count = len(tasks)
        result.last_synced_at = datetime.utcnow()
        # participated controls the rank / rating display
        result.participated = had_rated
        if entry:
            result.rank = entry.rank
            result.old_rating = entry.old_rating
            result.new_rating = entry.new_rating
            if entry.old_rating is not None and entry.new_rating is not None:
                result.rating_change = entry.new_rating - entry.old_rating

        for cp in existing.values():
            info = classification.get(cp.platform_problem_id, {})
            pr = db.query(models.ProblemResult).filter_by(contest_problem_id=cp.id, user_id=user.id).first()
            if not pr:
                pr = models.ProblemResult(contest_problem_id=cp.id, user_id=user.id)
                db.add(pr)
            pr.solved = info.get("solved", False)
            pr.solve_type = info.get("solve_type")
            pr.attempts = info.get("attempts") or None
            wv = info.get("wrong_verdicts", [])
            pr.best_wrong_verdict = " ".join(wv) if wv else None


# ── standalone problem sync ──────────────────────────────────────────────────

async def _sync_problem(item, members: list, db) -> None:
    parsed = api.parse_problem_id(item.external_id)
    if not parsed:
        raise ValueError(f"Cannot parse AtCoder problem ID: {item.external_id}")
    _, problem_id = parsed

    if not item.title:
        try:
            item.title = await api.get_problem_title(problem_id)
        except Exception:
            logger.warning("Could not fetch AtCoder title for %s", problem_id, exc_info=True)

    for user in members:
        if not submissions.is_synced(db, user.id, "atcoder"):
            continue
        subs = submissions.for_problem(db, user.id, "atcoder", problem_id)
        first_ac = next((s for s in subs if s.accepted), None)
        result = db.query(models.Result).filter_by(assignment_item_id=item.id, user_id=user.id).first()
        if not result:
            result = models.Result(assignment_item_id=item.id, user_id=user.id)
            db.add(result)
        result.solved = first_ac is not None
        result.solve_time = (
            datetime.fromtimestamp(first_ac.submitted_at, timezone.utc).replace(tzinfo=None) if first_ac else None
        )
        result.last_synced_at = datetime.utcnow()


PLATFORM = AtCoder()
