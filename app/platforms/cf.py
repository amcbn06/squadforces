"""Codeforces: regular contests, gyms and EDU practice problems.

Link recognition, presentation, submission fetching and the derivation of results from the stored submissions
all live here. The HTTP calls themselves are in app/scraper/codeforces.py.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import ParseResult, urlparse

from app import models, submissions
from app.platforms.base import KnownState, ParsedLink, Platform, RatingData, SubmissionData
from app.scraper import codeforces as api

logger = logging.getLogger(__name__)

# Sources this module recognises (see app/problems.py::detect_source)
CF = "cf"
CF_EDU = "cf_edu"
CF_GYM = "cf_gym"

_INDEX = r"[A-Za-z]\d?"
# /edu/course/2/lesson/4/3/practice/contest/274545/problem/A
_EDU_PROBLEM = re.compile(rf"^/edu/course/\d+/lesson/\d+(?:/\d+)*/practice/contest/(\d+)/problem/({_INDEX})/?$")
_PROBLEM_PATH = re.compile(rf"^/(?:problemset/(?:problem|gymProblem)|contest|gym)/(\d+)/(?:problem/)?({_INDEX})/?$")
_CONTEST_PATH = re.compile(r"^/(?:contest|gym)/(\d+)(?:/|$)")
_BARE_PROBLEM = re.compile(rf"^(\d+)[/\s]*({_INDEX})$")

# Contest ids: regular contests are numbered from 1; gyms start at 100001 and EDU practice contests sit above them.
GYM_MIN_ID = 100_000
GYM_MAX_ID = 200_000

FULL_HISTORY_COUNT = 100_000  # user.status count for the first, complete load
INCREMENTAL_PAGE = 200        # submissions per call while catching up
MAX_PAGES = 500               # safety stop for a runaway catch-up

_BLOCKED_STANDINGS = ("non-gym contest standings", "non-admin")  # errors of contests whose standings are restricted

_VERDICT_SHORT: dict[str, str] = {
    "WRONG_ANSWER":             "WA",
    "TIME_LIMIT_EXCEEDED":      "TLE",
    "MEMORY_LIMIT_EXCEEDED":    "MLE",
    "RUNTIME_ERROR":            "RE",
    "COMPILATION_ERROR":        "CE",
    "IDLENESS_LIMIT_EXCEEDED":  "ILE",
    "PRESENTATION_ERROR":       "PE",
    "CHALLENGED":               "HK",
    "FAILED":                   "FL",
}


def shorten_verdict(verdict: Optional[str]) -> str:
    """Codeforces verdict -> the short code shown in the UI ("" when there is none yet)."""
    return _VERDICT_SHORT.get(verdict, verdict[:3]) if verdict else ""


def is_gym_id(contest_id) -> bool:
    try:
        return GYM_MIN_ID <= int(contest_id) < GYM_MAX_ID
    except (TypeError, ValueError):
        return False


# ── URL predicates used by problems.detect_source ────────────────────────────

def _host_ok(url: ParseResult) -> bool:
    host = (url.hostname or "").lower()
    return host == "codeforces.com" or host.endswith(".codeforces.com")


def is_edu_url(url: ParseResult) -> bool:
    return _host_ok(url) and url.path.startswith("/edu/")


def is_gym_url(url: ParseResult) -> bool:
    return _host_ok(url) and (url.path.startswith("/gym/") or url.path.startswith("/problemset/gymProblem/"))


def is_url(url: ParseResult) -> bool:
    return _host_ok(url)


def is_bare_id(token: str) -> bool:
    return bool(_BARE_PROBLEM.match(token))


# ── parsing ──────────────────────────────────────────────────────────────────

def _problem_link(contest_id: str, index: str, source_url: Optional[str] = None) -> ParsedLink:
    ext = f"{contest_id}/{index.upper()}"
    return ParsedLink("codeforces", "problem", ext, f"Codeforces problem {ext}", source_url)


def _contest_link(contest_id: str) -> ParsedLink:
    return ParsedLink("codeforces", "contest", contest_id, f"Codeforces contest {contest_id}")


def parse_url(source: str, url: ParseResult) -> Optional[ParsedLink]:
    path = url.path
    m = _EDU_PROBLEM.match(path)
    if m:
        canonical = path.rstrip("/")[: -len(m.group(2))] + m.group(2).upper()
        return _problem_link(m.group(1), m.group(2), f"https://codeforces.com{canonical}")
    if source == CF_EDU:
        return None  # an EDU page that is not a single problem
    m = _PROBLEM_PATH.match(path)
    if m:
        return _problem_link(m.group(1), m.group(2))
    m = _CONTEST_PATH.match(path)
    if m:
        return _contest_link(m.group(1))
    return None


class Codeforces(Platform):
    key = "codeforces"
    label = "Codeforces"
    icon = "cf.png"
    handle_attr = "codeforces_handle"
    profile_url = "https://codeforces.com/profile/{handle}"
    supports_contests = True
    has_submissions = True

    # ── recognition ──────────────────────────────────────────────────────────

    def parse_url(self, source: str, url: ParseResult) -> Optional[ParsedLink]:
        return parse_url(source, url)

    def parse_bare(self, token: str) -> Optional[ParsedLink]:
        m = _BARE_PROBLEM.match(token)
        return _problem_link(m.group(1), m.group(2)) if m else None

    def parse_form(self, item_type: str, raw: str) -> tuple[Optional[ParsedLink], Optional[str]]:
        raw = raw.strip()
        if item_type == "contest":
            m = re.search(r"(?:contest|gym)/(\d+)", raw)
            if m:
                return _contest_link(m.group(1)), None
            if raw.isdigit():
                return _contest_link(raw), None
            return None, "Invalid Codeforces contest URL or ID."
        link = None
        if re.match(r"^(https?://|www\.|codeforces\.com/)", raw, re.I):
            url = urlparse(raw if re.match(r"^https?://", raw, re.I) else "https://" + raw)
            link = parse_url(CF_EDU if url.path.startswith("/edu/") else CF, url)
            if link and link.type != "problem":
                link = None
        else:
            link = self.parse_bare(raw)
        if link is None:
            return None, ("Invalid Codeforces problem URL or ID (e.g. 1234A or "
                          "https://codeforces.com/contest/1234/problem/A).")
        return link, None

    # ── presentation ─────────────────────────────────────────────────────────

    def item_url(self, item) -> str:
        contest_id, _, index = item.external_id.partition("/")
        gym = is_gym_id(contest_id)
        if item.type == "contest":
            return f"https://codeforces.com/{'gym' if gym else 'contest'}/{contest_id}"
        if item.source_url:
            return item.source_url
        if gym:
            return f"https://codeforces.com/gym/{contest_id}/problem/{index}"
        return f"https://codeforces.com/problemset/problem/{contest_id}/{index}"

    def problem_keys(self, item) -> set[str]:
        if item.type == "problem":
            return {item.external_id}
        return {f"{item.external_id}/{cp.index}" for cp in item.contest_problems}

    def problem_url(self, item, contest_problem) -> str:
        if is_gym_id(item.external_id):
            return f"https://codeforces.com/gym/{item.external_id}/problem/{contest_problem.index}"
        return f"https://codeforces.com/problemset/problem/{item.external_id}/{contest_problem.index}"

    def submission_url(self, sub) -> str:
        contest = sub.contest_key or sub.problem_key.partition("/")[0]
        return f"https://codeforces.com/{'gym' if is_gym_id(contest) else 'contest'}/{contest}/submission/{sub.submission_id}"

    def submission_problem_url(self, sub) -> str:
        contest, _, index = sub.problem_key.partition("/")
        if is_gym_id(contest):
            return f"https://codeforces.com/gym/{contest}/problem/{index}"
        return f"https://codeforces.com/problemset/problem/{contest}/{index}"

    def submission_problem_label(self, sub) -> str:
        return f"{sub.problem_key.replace('/', '')}. {sub.problem_name}" if sub.problem_name else sub.problem_key

    def manual_status(self, item) -> bool:
        return bool(item.source_url)  # EDU practice problems: not exposed by the API

    def default_title(self, item) -> str:
        return f"Contest {item.external_id}" if item.type == "contest" else item.external_id

    # ── fetching ─────────────────────────────────────────────────────────────

    async def fetch_submissions(self, handle: str, known: KnownState) -> list[SubmissionData]:
        if known.stop_id is None:
            raw = await api.get_user_status(handle, 1, FULL_HISTORY_COUNT)
            return _convert_all(raw)

        out: list[SubmissionData] = []
        offset = 1
        for _ in range(MAX_PAGES):
            batch = await api.get_user_status(handle, offset, INCREMENTAL_PAGE)
            out.extend(_convert_all(batch))
            if len(batch) < INCREMENTAL_PAGE:
                break
            if min(s["id"] for s in batch) <= known.stop_id:
                break  # reached what the store already has
            offset += INCREMENTAL_PAGE
        return out

    async def fetch_rating_history(self, handle: str) -> list[RatingData]:
        return [
            RatingData(
                contest_key=str(h["contestId"]),
                contest_name=h.get("contestName"),
                rank=h.get("rank"),
                old_rating=h.get("oldRating"),
                new_rating=h.get("newRating"),
                rated_at=h.get("ratingUpdateTimeSeconds"),
            )
            for h in await api.get_user_rating_history(handle)
        ]

    # ── item sync ────────────────────────────────────────────────────────────

    async def sync_item(self, item, members: list, db) -> None:
        handled = [u for u in members if u.codeforces_handle]
        if item.type == "contest":
            await _sync_contest(item, handled, db)
        else:
            await _sync_problem(item, handled, db)


def _convert(raw: dict) -> Optional[SubmissionData]:
    """One user.status entry -> SubmissionData (None if it has no usable problem)."""
    prob = raw.get("problem") or {}
    contest_id, index = prob.get("contestId"), prob.get("index")
    if contest_id is None or not index or "id" not in raw:
        return None
    author = raw.get("author") or {}
    verdict = raw.get("verdict")
    accepted = verdict == "OK"
    team_id = author.get("teamId")
    return SubmissionData(
        submission_id=raw["id"],
        problem_key=f"{contest_id}/{index}",
        submitted_at=raw.get("creationTimeSeconds", 0),
        verdict="AC" if accepted else shorten_verdict(verdict),
        accepted=accepted,
        final=verdict not in (None, "TESTING"),
        contest_key=str(raw["contestId"]) if raw.get("contestId") is not None else None,
        problem_index=index,
        problem_name=prob.get("name"),
        problem_rating=prob.get("rating"),
        relative_seconds=raw.get("relativeTimeSeconds"),
        participant_type=author.get("participantType") or "PRACTICE",
        team_id=str(team_id) if team_id is not None else None,
        team_name=author.get("teamName"),
        language=raw.get("programmingLanguage"),
    )


def _convert_all(raw: list[dict]) -> list[SubmissionData]:
    return [d for d in map(_convert, raw) if d]


# ── classification (live / virtual / upsolved) ───────────────────────────────

def classify_contest(subs) -> tuple[dict[str, dict], bool, bool]:
    """Classify one user's submissions for one contest.

    `subs` are stored submissions (anything with problem_index, participant_type, verdict, accepted and
    submitted_at). Returns:
        per_problem: {index: {solved, solve_type, attempts, wrong_verdicts}}
        did_live: the user had CONTESTANT submissions
        did_virtual: the user had VIRTUAL submissions

    solve_type: "live" | "virtual" | "upsolving" | "standalone" | None
    attempts: wrong submissions before the first AC (or all of them if never AC'd)
    wrong_verdicts: short verdicts (WA, TLE...) seen before the AC, or overall when not solved
    """
    subs = sorted(subs, key=lambda s: (s.submitted_at, s.submission_id))

    ptypes_seen = {s.participant_type or "" for s in subs}
    did_live = "CONTESTANT" in ptypes_seen
    did_virtual = "VIRTUAL" in ptypes_seen

    # Group by (problem_index, participantType)
    by_prob: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for s in subs:
        if s.problem_index:
            by_prob[s.problem_index][s.participant_type or "PRACTICE"].append(s)

    per_problem: dict[str, dict] = {}

    for idx, by_type in by_prob.items():
        # Per-type stats — wrong verdicts, first AC position and its timestamp
        type_stats: dict[str, dict] = {}
        for ptype, type_subs in by_type.items():
            wrong: list[str] = []
            ac_pos: Optional[int] = None
            ac_time = 0
            for i, s in enumerate(type_subs):
                if s.accepted:
                    ac_pos = i
                    ac_time = s.submitted_at
                    break
                if s.verdict and s.verdict not in wrong:
                    wrong.append(s.verdict)
            type_stats[ptype] = {
                "solved": ac_pos is not None,
                "attempts": ac_pos if ac_pos is not None else len(type_subs),
                "wrong": wrong,
                "ac_time": ac_time,
            }

        # Most-recent solve wins (recency > participation-type priority)
        solved_types = [(ptype, info) for ptype, info in type_stats.items() if info["solved"]]

        solved = False
        solve_type = None
        attempts = 0
        wrong_verdicts: list[str] = []

        if solved_types:
            best_ptype, best_info = max(solved_types, key=lambda x: x[1]["ac_time"])
            solved = True
            attempts = best_info["attempts"]
            wrong_verdicts = best_info["wrong"]
            if best_ptype == "CONTESTANT":
                solve_type = "live"
            elif best_ptype == "VIRTUAL":
                solve_type = "virtual"
            else:  # PRACTICE
                solve_type = "upsolving" if (did_live or did_virtual) else "standalone"
        else:
            # Not solved — aggregate wrong verdicts and attempt counts across all types
            seen: set[str] = set()
            for ptype in ("CONTESTANT", "VIRTUAL", "PRACTICE"):
                info = type_stats.get(ptype)
                if not info:
                    continue
                attempts += info["attempts"]
                for v in info["wrong"]:
                    if v not in seen:
                        seen.add(v)
                        wrong_verdicts.append(v)

        per_problem[idx] = {
            "solved": solved,
            "solve_type": solve_type,
            "attempts": attempts,
            "wrong_verdicts": wrong_verdicts,
        }

    return per_problem, did_live, did_virtual


# ── contest sync ─────────────────────────────────────────────────────────────

_RATING_RECHECK_HOURS = 6  # a contest's problem ratings appear some time after it ends


async def _contest_meta(item) -> tuple[str, list[dict]]:
    """(title, problems) from Codeforces. Raises ContestNotStartedError for a contest that hasn't begun."""
    contest_id = item.external_id
    if is_gym_id(contest_id):
        meta = await api.get_gym_metadata(contest_id)
        title = (meta or {}).get("name", "")
        if meta and meta.get("phase") == "BEFORE":
            raise api.ContestNotStartedError(f"Contest {contest_id} has not started yet")
        return title, await api.get_gym_problems(contest_id)
    try:
        info = await api.get_contest_info(contest_id)
        return info["title"], info["problems"]
    except api.ContestNotStartedError:
        meta = await api.get_contest_metadata(contest_id)  # name for the "not started yet" row
        if meta and not item.title:
            item.title = meta.get("name") or f"Contest {contest_id}"
        raise
    except ValueError as e:
        if not any(k in str(e).lower() for k in _BLOCKED_STANDINGS):
            raise
        # Standings restricted: fall back to whatever contest.status reveals
        return "", await api.get_contest_problems_from_status(contest_id)


def _needs_meta(item) -> bool:
    problems = item.contest_problems
    if not problems or not item.title:
        return True
    if is_gym_id(item.external_id):
        return False  # gyms never get ratings, and their problem list doesn't change
    if any(p.rating is None for p in problems) and item.last_synced_at is not None:
        return (datetime.utcnow() - item.last_synced_at).total_seconds() > _RATING_RECHECK_HOURS * 3600
    return False


def _ensure_problem(db, item, existing: dict, index: str, name: str, rating) -> None:
    cp = existing.get(index)
    if cp is None:
        cp = models.ContestProblem(
            assignment_item_id=item.id,
            platform_problem_id=f"{item.external_id}{index}",
            index=index,
            name=name or "",
            rating=rating,
        )
        db.add(cp)
        db.flush()
        existing[index] = cp
    else:
        if name:
            cp.name = name
        if rating:
            cp.rating = rating


async def _sync_contest(item, members: list, db) -> None:
    if not members:
        return
    platform_key = "codeforces"

    problems: list[dict] = []
    if _needs_meta(item):
        try:
            title, problems = await _contest_meta(item)
            if not item.title:
                item.title = title or f"Contest {item.external_id}"
        except api.ContestNotStartedError:
            raise  # sync_item marks the item "not_started"
        except Exception:
            logger.warning("Could not load Codeforces contest %s metadata", item.external_id, exc_info=True)
    if not item.title:
        item.title = f"Contest {item.external_id}"

    existing: dict[str, models.ContestProblem] = {cp.index: cp for cp in item.contest_problems}
    for prob in problems:
        if prob.get("index"):
            _ensure_problem(db, item, existing, prob["index"], prob.get("name", ""), prob.get("rating"))

    # ── per-user results, from the stored submissions ────────────────────────
    synced_members = [u for u in members if submissions.is_synced(db, u.id, platform_key)]
    classified: dict[int, tuple[dict, bool, bool, list]] = {}
    for user in synced_members:
        subs = submissions.for_contest(db, user.id, platform_key, item.external_id)
        # Problems the contest listing didn't give (e.g. standings were blocked) come from what people submitted
        for sub in subs:
            if sub.problem_index and sub.problem_index not in existing:
                _ensure_problem(db, item, existing, sub.problem_index, sub.problem_name or "", sub.problem_rating)
        classified[user.id] = (*classify_contest(subs), subs)

    for user in synced_members:
        classification, did_live, did_virtual, subs = classified[user.id]

        result = db.query(models.Result).filter_by(assignment_item_id=item.id, user_id=user.id).first()
        if not result:
            result = models.Result(assignment_item_id=item.id, user_id=user.id)
            db.add(result)

        result.participated = did_live or did_virtual or bool(subs)
        result.last_synced_at = datetime.utcnow()
        result.problems_solved_count = sum(1 for info in classification.values() if info["solved"])
        result.problems_total_count = len(existing) or None

        for idx, cp in existing.items():
            info = classification.get(idx, {})
            pr = db.query(models.ProblemResult).filter_by(contest_problem_id=cp.id, user_id=user.id).first()
            if not pr:
                pr = models.ProblemResult(contest_problem_id=cp.id, user_id=user.id)
                db.add(pr)
            pr.solved = info.get("solved", False)
            pr.solve_type = info.get("solve_type")
            pr.attempts = info.get("attempts", 0) or None
            wv = info.get("wrong_verdicts", [])
            pr.best_wrong_verdict = " ".join(wv) if wv else None

        entry = submissions.rating_entry(db, user.id, platform_key, item.external_id)
        if entry:
            result.old_rating = entry.old_rating
            result.new_rating = entry.new_rating
            if entry.old_rating is not None and entry.new_rating is not None:
                result.rating_change = entry.new_rating - entry.old_rating

    db.flush()

    # A problem found in one member's submissions needs a row for everyone, or the matrix shows gaps.
    for user in synced_members:
        for cp in existing.values():
            if not db.query(models.ProblemResult).filter_by(contest_problem_id=cp.id, user_id=user.id).first():
                db.add(models.ProblemResult(contest_problem_id=cp.id, user_id=user.id, solved=False))
    db.flush()


# ── standalone problem sync ──────────────────────────────────────────────────

async def _sync_problem(item, members: list, db) -> None:
    contest_id, _, index = item.external_id.partition("/")
    if not index:
        raise ValueError(f"Cannot parse CF problem ID: {item.external_id}")
    platform_key = "codeforces"

    # EDU practice contests are invisible to the public API (contest.* says "not found" and user.status omits
    # their submissions), so recording "not solved" would be wrong for people who did solve it.
    if item.source_url:
        return

    synced = [u for u in members if submissions.is_synced(db, u.id, platform_key)]
    per_user = {u.id: submissions.for_problem(db, u.id, platform_key, item.external_id) for u in synced}

    if not item.title:
        name = next((s.problem_name for subs in per_user.values() for s in subs if s.problem_name), None)
        if name:
            item.title = name
        elif is_gym_id(contest_id):
            try:
                names = {p["index"]: p["name"] for p in await api.get_gym_problems(contest_id)}
                item.title = names.get(index) or f"CF {item.external_id}"
            except Exception:
                item.title = f"CF {item.external_id}"
        else:
            try:
                for p in await api.get_contest_problems(contest_id):
                    if p.get("index") == index:
                        item.title = p.get("name", f"Problem {index}")
                        break
            except Exception:
                pass
            item.title = item.title or f"CF {item.external_id}"

    if item.rating is None and not is_gym_id(contest_id):
        try:
            item.rating = await api.get_problem_rating(contest_id, index)
        except Exception:
            logger.warning("Could not fetch CF rating for %s", item.external_id, exc_info=True)

    for user in synced:
        subs = per_user[user.id]
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


PLATFORM = Codeforces()
