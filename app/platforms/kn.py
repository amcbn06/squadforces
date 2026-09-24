"""Kilonova (kilonova.ro): problems and problem lists ("contests" here)."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import ParseResult

from app import models, submissions
from app.platforms.base import KnownState, ParsedLink, Platform, SubmissionData
from app.scraper import kilonova as api

logger = logging.getLogger(__name__)

KN = "kn"

_PROBLEM_PATH = re.compile(r"^/problems/(\d+)(?:/|$)")
_LIST_PATH = re.compile(r"^/problem_lists/(\d+)(?:/|$)")
DEFAULT_SCALE = 100


def is_url(url: ParseResult) -> bool:
    host = (url.hostname or "").lower()
    return host == "kilonova.ro" or host.endswith(".kilonova.ro")


def _problem_link(pid: str) -> ParsedLink:
    return ParsedLink("kilonova", "problem", pid, f"Kilonova problem {pid}")


def _list_link(list_id: str) -> ParsedLink:
    return ParsedLink("kilonova", "contest", list_id, f"Kilonova contest {list_id}")


def _epoch(created_at: str) -> int:
    """Kilonova timestamps look like 2026-05-09T13:37:53.160257+02:00."""
    try:
        return int(datetime.fromisoformat(created_at).timestamp())
    except ValueError:
        return int(datetime.fromisoformat(re.sub(r"\.\d+", "", created_at)).timestamp())


def _verdict(sub: dict, accepted: bool) -> str:
    if sub.get("status") != "finished":
        return ""
    if sub.get("compile_error"):
        return "CE"
    if accepted:
        return "AC"
    if (sub.get("score") or 0) > 0:
        return "PT"
    text = (sub.get("icpc_verdict") or "").lower()
    if "time limit" in text:
        return "TLE"
    if "memory" in text:
        return "MLE"
    if "runtime" in text:
        return "RE"
    return "WA"


def _convert(sub: dict) -> Optional[SubmissionData]:
    if "id" not in sub or "problem_id" not in sub or not sub.get("created_at"):
        return None
    score = sub.get("score") or 0
    scale = sub.get("score_scale") or DEFAULT_SCALE
    finished = sub.get("status") == "finished"
    accepted = finished and not sub.get("compile_error") and score >= scale
    return SubmissionData(
        submission_id=sub["id"],
        problem_key=str(sub["problem_id"]),
        submitted_at=_epoch(sub["created_at"]),
        verdict=_verdict(sub, accepted),
        accepted=accepted,
        final=finished,
        contest_key=str(sub["contest_id"]) if sub.get("contest_id") else None,
        score=float(score),
        max_score=float(scale),
        language=sub.get("language"),
    )


class Kilonova(Platform):
    key = "kilonova"
    label = "Kilonova"
    icon = "kn.png"
    handle_attr = "kilonova_handle"
    profile_url = "https://kilonova.ro/user/{handle}"
    supports_contests = True
    has_submissions = True

    # ── recognition ──────────────────────────────────────────────────────────

    def parse_url(self, source: str, url: ParseResult) -> Optional[ParsedLink]:
        m = _PROBLEM_PATH.match(url.path)
        if m:
            return _problem_link(m.group(1))
        m = _LIST_PATH.match(url.path)
        if m:
            return _list_link(m.group(1))
        return None

    def parse_form(self, item_type: str, raw: str) -> tuple[Optional[ParsedLink], Optional[str]]:
        if item_type == "contest":
            parsed = api.parse_contest_id(raw)
            if parsed is None:
                return None, ("Invalid Kilonova problem list URL or ID "
                              "(e.g. 1572 or https://kilonova.ro/problem_lists/1572).")
            return _list_link(str(parsed)), None
        parsed = api.parse_problem_id(raw)
        if parsed is None:
            return None, "Invalid Kilonova problem URL or ID (e.g. 2460 or https://kilonova.ro/problems/2460)."
        return _problem_link(str(parsed)), None

    # ── presentation ─────────────────────────────────────────────────────────

    def item_url(self, item) -> str:
        if item.type == "contest":
            return f"https://kilonova.ro/problem_lists/{item.external_id}"
        return f"https://kilonova.ro/problems/{item.external_id}"

    def problem_url(self, item, contest_problem) -> str:
        return f"https://kilonova.ro/problems/{contest_problem.platform_problem_id}"

    def default_title(self, item) -> str:
        return f"List {item.external_id}" if item.type == "contest" else item.external_id

    def submission_url(self, sub) -> str:
        return f"https://kilonova.ro/submissions/{sub.submission_id}"

    def submission_problem_url(self, sub) -> str:
        return f"https://kilonova.ro/problems/{sub.problem_key}"

    def submission_problem_label(self, sub) -> str:
        return f"Problem {sub.problem_key}"

    # ── fetching ─────────────────────────────────────────────────────────────

    async def fetch_submissions(self, handle: str, known: KnownState) -> list[SubmissionData]:
        user_id = await api.get_user_id(handle)
        if not user_id:
            raise ValueError(f"Kilonova user '{handle}' was not found")
        out: list[SubmissionData] = []
        offset = 0
        while True:  # newest first, 50 per call
            page, total = await api.get_user_submissions(user_id, offset)
            if not page:
                break
            out.extend(d for d in map(_convert, page) if d)
            offset += len(page)
            if offset >= total:
                break
            if known.stop_id is not None and min(s["id"] for s in page) <= known.stop_id:
                break  # reached what the store already has
        return out

    # ── item sync ────────────────────────────────────────────────────────────

    async def sync_item(self, item, members: list, db) -> None:
        handled = [u for u in members if u.kilonova_handle]
        if item.type == "contest":
            await _sync_contest(item, handled, db)
        else:
            await _sync_problem(item, handled, db)


def _best(subs, key: str) -> float:
    """Highest score among a user's submissions to one problem (0 when there are none)."""
    return max((s.score or 0 for s in subs if s.problem_key == key), default=0)


async def _sync_contest(item, members: list, db) -> None:
    list_id = int(item.external_id)
    pl = await api.get_problem_list(list_id)
    if not item.title:
        item.title = pl.get("title") or f"Kilonova list {list_id}"

    problem_ids: list[int] = pl.get("list", [])
    existing = {cp.platform_problem_id: cp for cp in item.contest_problems}
    for i, pid in enumerate(problem_ids):
        key = str(pid)
        cp = existing.get(key)
        if cp is None or cp.max_score is None:
            info = await api.get_problem(pid)  # only for problems not seen before
            name = info.get("name", f"Problem {pid}")
            scale = info.get("score_scale") or DEFAULT_SCALE
            if cp is None:
                cp = models.ContestProblem(
                    assignment_item_id=item.id, platform_problem_id=key, index=str(i + 1), name=name,
                    rating=None, max_score=scale,
                )
                db.add(cp)
                db.flush()
                existing[key] = cp
            else:
                cp.name, cp.max_score = name, scale
    listed = [existing[str(pid)] for pid in problem_ids]

    scale_total = sum(cp.max_score or DEFAULT_SCALE for cp in listed)
    keys = {cp.platform_problem_id for cp in listed}

    for user in members:
        if not submissions.is_synced(db, user.id, "kilonova"):
            continue
        subs = submissions.for_problems(db, user.id, "kilonova", keys)

        result = db.query(models.Result).filter_by(assignment_item_id=item.id, user_id=user.id).first()
        if not result:
            result = models.Result(assignment_item_id=item.id, user_id=user.id)
            db.add(result)

        solved_count = 0
        score_total = 0
        for cp in listed:
            best = _best(subs, cp.platform_problem_id)
            scale = cp.max_score or DEFAULT_SCALE
            solved = best >= scale
            score_total += int(best)
            solved_count += solved

            pr = db.query(models.ProblemResult).filter_by(contest_problem_id=cp.id, user_id=user.id).first()
            if not pr:
                pr = models.ProblemResult(contest_problem_id=cp.id, user_id=user.id)
                db.add(pr)
            pr.solved = solved
            pr.score = int(best)
            pr.solve_type = None
            pr.attempts = None
            pr.best_wrong_verdict = None

        result.problems_solved_count = solved_count
        result.problems_total_count = len(problem_ids)
        result.participated = None
        result.raw_scrape_data = {"kn_score": score_total, "kn_total": scale_total}
        result.last_synced_at = datetime.utcnow()


async def _sync_problem(item, members: list, db) -> None:
    problem_id = int(item.external_id)
    info = await api.get_problem(problem_id)
    if not item.title:
        item.title = info.get("name") or f"Kilonova {problem_id}"
    scale = info.get("score_scale") or DEFAULT_SCALE

    for user in members:
        if not submissions.is_synced(db, user.id, "kilonova"):
            continue
        subs = submissions.for_problem(db, user.id, "kilonova", item.external_id)
        best = max((s.score or 0 for s in subs), default=0)
        first_full = next((s for s in subs if (s.score or 0) >= scale), None)

        result = db.query(models.Result).filter_by(assignment_item_id=item.id, user_id=user.id).first()
        if not result:
            result = models.Result(assignment_item_id=item.id, user_id=user.id)
            db.add(result)
        result.solved = best >= scale
        result.solve_time = (
            datetime.fromtimestamp(first_full.submitted_at, timezone.utc).replace(tzinfo=None) if first_full else None
        )
        result.raw_scrape_data = {"kn_score": int(best), "kn_total": scale}
        result.last_synced_at = datetime.utcnow()


PLATFORM = Kilonova()
