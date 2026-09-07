"""Sync service — fetches data from CF/AtCoder APIs and writes Results to DB."""
from collections import defaultdict
from datetime import datetime
from sqlalchemy.orm import Session
from app import models
from app.scraper import codeforces as cf
from app.scraper import atcoder as ac

# ── Verdict shortening ────────────────────────────────────────────────────────

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


def _shorten_verdict(v: str) -> str:
    return _VERDICT_SHORT.get(v, v[:3]) if v else ""


# ── Submission classifier ─────────────────────────────────────────────────────

def _classify_submissions(subs: list[dict]) -> tuple[dict[str, dict], bool, bool]:
    """
    Classify all of a user's submissions for one contest.

    Returns:
        per_problem: {index: {solved, solve_type, attempts, wrong_verdicts}}
        did_live: user had CONTESTANT submissions
        did_virtual: user had VIRTUAL submissions

    solve_type: "live" | "virtual" | "upsolving" | "standalone" | None
    attempts: wrong submissions before first AC (or total wrong if never AC'd)
    wrong_verdicts: list of short verdict strings (WA, TLE…) — non-empty only when not solved
    """
    subs = sorted(subs, key=lambda s: s.get("creationTimeSeconds", 0))

    ptypes_seen: set[str] = {
        s.get("author", {}).get("participantType", "") for s in subs
    }
    did_live = "CONTESTANT" in ptypes_seen
    did_virtual = "VIRTUAL" in ptypes_seen

    # Group by (problem_index, participantType)
    by_prob: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for s in subs:
        idx = s.get("problem", {}).get("index", "")
        ptype = s.get("author", {}).get("participantType", "PRACTICE")
        if idx:
            by_prob[idx][ptype].append(s)

    per_problem: dict[str, dict] = {}

    for idx, by_type in by_prob.items():
        # Per-type stats — collect wrong verdicts, first AC position, and AC timestamp
        type_stats: dict[str, dict] = {}
        for ptype, type_subs in by_type.items():
            wrong: list[str] = []
            ac_pos: int | None = None
            ac_time: int = 0
            for i, s in enumerate(type_subs):
                verdict = s.get("verdict", "")
                if verdict == "OK":
                    ac_pos = i
                    ac_time = s.get("creationTimeSeconds", 0)
                    break
                short = _shorten_verdict(verdict)
                if short and short not in wrong:
                    wrong.append(short)
            type_stats[ptype] = {
                "solved": ac_pos is not None,
                "attempts": ac_pos if ac_pos is not None else len(type_subs),
                "wrong": wrong,
                "ac_time": ac_time,
            }

        # Most-recent solve wins (recency > participation-type priority)
        solved_types = [
            (ptype, info) for ptype, info in type_stats.items() if info["solved"]
        ]

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


# ── Top-level sync entry point ────────────────────────────────────────────────

async def sync_item(item_id: int, db: Session) -> None:
    item = db.get(models.AssignmentItem, item_id)
    if not item:
        return

    item.sync_status = "syncing"
    item.sync_error = None
    db.commit()

    try:
        if item.platform == "codeforces":
            await _sync_cf_item(item, db)
        elif item.platform == "atcoder":
            await _sync_ac_item(item, db)

        item.last_synced_at = datetime.utcnow()
        item.sync_status = "done"
    except Exception as exc:
        item.sync_status = "error"
        item.sync_error = str(exc)

    db.commit()


async def _sync_cf_item(item: models.AssignmentItem, db: Session) -> None:
    assignment = db.get(models.Assignment, item.assignment_id)
    group = db.get(models.Group, assignment.group_id)
    members = [m.user for m in group.memberships]
    handles = [u.codeforces_handle for u in members]

    if item.type == "contest":
        await _sync_cf_contest(item, members, handles, db)
    else:
        await _sync_cf_problem(item, members, db)


_CF_BLOCKED = ("non-gym contest standings", "non-admin")


async def _sync_cf_contest(
    item: models.AssignmentItem,
    members: list,
    handles: list[str],
    db: Session,
) -> None:
    if not handles:
        return

    handle_map = {u.codeforces_handle: u for u in members}

    # ── Step 1: Get problem list ───────────────────────────────────────────────
    problems: list[dict] = []
    try:
        info = await cf.get_contest_info(item.external_id)
        problems = info["problems"]
        if not item.title:
            item.title = info["title"] or f"Contest {item.external_id}"
    except ValueError as e:
        if not any(k in str(e).lower() for k in _CF_BLOCKED):
            raise
        # 24-hour restriction: fall back to contest.status to discover problems
        problems = await cf.get_contest_problems_from_status(item.external_id)
    except Exception:
        pass

    if not item.title:
        item.title = f"Contest {item.external_id}"

    # Upsert ContestProblems
    existing_problems: dict[str, models.ContestProblem] = {
        cp.index: cp for cp in item.contest_problems
    }
    for prob in problems:
        idx = prob.get("index", "")
        if not idx:
            continue
        if idx not in existing_problems:
            cp = models.ContestProblem(
                assignment_item_id=item.id,
                platform_problem_id=f"{item.external_id}{idx}",
                index=idx,
                name=prob.get("name", ""),
                rating=prob.get("rating"),
            )
            db.add(cp)
            db.flush()
            existing_problems[idx] = cp
        else:
            existing_problems[idx].name = prob.get("name", existing_problems[idx].name)
            if prob.get("rating"):
                existing_problems[idx].rating = prob["rating"]

    # ── Step 2: Per-user results via user.status ──────────────────────────────
    for handle, user in handle_map.items():
        subs = await cf.get_user_submissions_for_contest(handle, item.external_id)

        # Discover problems not in the problem list (e.g. when standings were blocked)
        for sub in subs:
            prob = sub.get("problem", {})
            idx = prob.get("index", "")
            if idx and idx not in existing_problems:
                cp = models.ContestProblem(
                    assignment_item_id=item.id,
                    platform_problem_id=f"{item.external_id}{idx}",
                    index=idx,
                    name=prob.get("name", ""),
                    rating=prob.get("rating"),
                )
                db.add(cp)
                db.flush()
                existing_problems[idx] = cp

        classification, did_live, did_virtual = _classify_submissions(subs)

        # Upsert Result
        result = (
            db.query(models.Result)
            .filter_by(assignment_item_id=item.id, user_id=user.id)
            .first()
        )
        if not result:
            result = models.Result(assignment_item_id=item.id, user_id=user.id)
            db.add(result)

        participated = did_live or did_virtual or bool(subs)
        result.participated = participated
        result.last_synced_at = datetime.utcnow()

        solved_indices = {
            idx for idx, info in classification.items() if info["solved"]
        }
        result.problems_solved_count = len(solved_indices)
        result.problems_total_count = len(existing_problems) or None

        # Upsert ProblemResult per problem
        for idx, cp in existing_problems.items():
            info = classification.get(idx, {})
            pr_record = (
                db.query(models.ProblemResult)
                .filter_by(contest_problem_id=cp.id, user_id=user.id)
                .first()
            )
            if not pr_record:
                pr_record = models.ProblemResult(
                    contest_problem_id=cp.id, user_id=user.id
                )
                db.add(pr_record)

            pr_record.solved = info.get("solved", False)
            pr_record.solve_type = info.get("solve_type")
            pr_record.attempts = info.get("attempts", 0) or None
            wv = info.get("wrong_verdicts", [])
            pr_record.best_wrong_verdict = " ".join(wv) if wv else None

    db.flush()

    # ── Step 2b: Back-fill missing ProblemResult rows ─────────────────────────
    # A problem discovered from user B's subs won't have a row for user A if
    # user A was already processed. Fill gaps so every user has a row per problem.
    for handle, user in handle_map.items():
        for idx, cp in existing_problems.items():
            exists = (
                db.query(models.ProblemResult)
                .filter_by(contest_problem_id=cp.id, user_id=user.id)
                .first()
            )
            if not exists:
                db.add(models.ProblemResult(
                    contest_problem_id=cp.id, user_id=user.id,
                    solved=False,
                ))

    db.flush()

    # ── Step 3: Rating changes ─────────────────────────────────────────────────
    for handle, user in handle_map.items():
        try:
            history = await cf.get_user_rating_history(handle)
            for entry in history:
                if str(entry.get("contestId")) == str(item.external_id):
                    result = (
                        db.query(models.Result)
                        .filter_by(assignment_item_id=item.id, user_id=user.id)
                        .first()
                    )
                    if result:
                        result.old_rating = entry.get("oldRating")
                        result.new_rating = entry.get("newRating")
                        result.rating_change = (
                            entry.get("newRating", 0) - entry.get("oldRating", 0)
                        )
                    break
        except Exception:
            pass


async def _sync_cf_problem(
    item: models.AssignmentItem,
    members: list,
    db: Session,
) -> None:
    parsed = cf.parse_problem_external_id(item.external_id)
    if not parsed:
        raise ValueError(f"Cannot parse CF problem ID: {item.external_id}")
    contest_id, index = parsed

    if not item.title:
        try:
            probs = await cf.get_contest_problems(contest_id)
            for p in probs:
                if p.get("index") == index:
                    item.title = p.get("name", f"Problem {index}")
                    break
        except Exception:
            item.title = f"CF {item.external_id}"

    for user in members:
        solved = await cf.get_problem_solved(user.codeforces_handle, contest_id, index)

        result = (
            db.query(models.Result)
            .filter_by(assignment_item_id=item.id, user_id=user.id)
            .first()
        )
        if not result:
            result = models.Result(assignment_item_id=item.id, user_id=user.id)
            db.add(result)

        result.solved = solved
        result.last_synced_at = datetime.utcnow()


async def _sync_ac_item(item: models.AssignmentItem, db: Session) -> None:
    assignment = db.get(models.Assignment, item.assignment_id)
    group = db.get(models.Group, assignment.group_id)
    members = [m.user for m in group.memberships if m.user.atcoder_handle]

    if item.type == "contest":
        await _sync_ac_contest(item, members, db)
    else:
        await _sync_ac_problem(item, members, db)


async def _sync_ac_contest(
    item: models.AssignmentItem,
    members: list,
    db: Session,
) -> None:
    tasks = await ac.get_contest_tasks(item.external_id)
    if not item.title:
        import re as _re
        _m = _re.match(r'^(abc|arc|agc)(\d+)$', item.external_id.lower())
        if _m:
            _names = {"abc": "AtCoder Beginner Contest", "arc": "AtCoder Regular Contest", "agc": "AtCoder Grand Contest"}
            item.title = f"{_names[_m.group(1)]} {int(_m.group(2))}"
        else:
            item.title = item.external_id.upper()

    existing_problems = {cp.index: cp for cp in item.contest_problems}
    for task in tasks:
        task_id = task.get("id", "")
        idx = task_id.split("_")[-1].upper() if "_" in task_id else task_id[-1].upper()
        raw_title = task.get("title", task_id)
        # kenkoooo titles include a leading "X. " prefix — strip it
        import re as _re
        name = _re.sub(r'^[A-Za-z]\.\s*', '', raw_title) or raw_title
        if idx not in existing_problems:
            cp = models.ContestProblem(
                assignment_item_id=item.id,
                platform_problem_id=task_id,
                index=idx,
                name=name,
                rating=task.get("difficulty"),
            )
            db.add(cp)
            db.flush()
            existing_problems[idx] = cp
        else:
            existing_problems[idx].name = name
            if task.get("difficulty"):
                existing_problems[idx].rating = int(round(task["difficulty"]))

    for user in members:
        if not user.atcoder_handle:
            continue
        subs = await ac.get_user_submissions(user.atcoder_handle)
        ac_solved = {s["problem_id"] for s in subs if s.get("result") == "AC"}

        result = (
            db.query(models.Result)
            .filter_by(assignment_item_id=item.id, user_id=user.id)
            .first()
        )
        if not result:
            result = models.Result(assignment_item_id=item.id, user_id=user.id)
            db.add(result)

        solved_count = 0
        result.problems_total_count = len(tasks)
        result.last_synced_at = datetime.utcnow()
        result.participated = True

        for idx, cp in existing_problems.items():
            is_solved = cp.platform_problem_id in ac_solved
            if is_solved:
                solved_count += 1
            pr_record = (
                db.query(models.ProblemResult)
                .filter_by(contest_problem_id=cp.id, user_id=user.id)
                .first()
            )
            if not pr_record:
                pr_record = models.ProblemResult(
                    contest_problem_id=cp.id, user_id=user.id
                )
                db.add(pr_record)
            pr_record.solved = is_solved

        result.problems_solved_count = solved_count

        try:
            contest_result = await ac.get_contest_results(item.external_id, user.atcoder_handle)
            if contest_result:
                result.old_rating = contest_result.get("OldRating")
                result.new_rating = contest_result.get("NewRating")
                result.rating_change = contest_result.get("InnerPerformance")
                result.rank = contest_result.get("Place")
        except Exception:
            pass


async def _sync_ac_problem(
    item: models.AssignmentItem,
    members: list,
    db: Session,
) -> None:
    parsed = ac.parse_problem_id(item.external_id)
    if not parsed:
        raise ValueError(f"Cannot parse AtCoder problem ID: {item.external_id}")
    _, problem_id = parsed

    for user in members:
        if not user.atcoder_handle:
            continue
        solved = await ac.check_problem_solved(user.atcoder_handle, problem_id)
        result = (
            db.query(models.Result)
            .filter_by(assignment_item_id=item.id, user_id=user.id)
            .first()
        )
        if not result:
            result = models.Result(assignment_item_id=item.id, user_id=user.id)
            db.add(result)
        result.solved = solved
        result.last_synced_at = datetime.utcnow()
