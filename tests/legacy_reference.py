"""The classifiers exactly as they were before the submission store existed (copied from master's app/sync.py).

Kept only as a reference: tests/test_classify.py feeds the same submissions to these and to the new classifiers
in app/platforms/ and demands identical answers, which is what "preserve the live/virtual/upsolved logic" means.
Do not edit; do not import from app code.
"""
from collections import defaultdict

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


def _classify_ac_submissions(
    subs: list[dict],
    problem_ids: set[str],
    contest_start: int,
    contest_end: int,
    had_rated: bool,
) -> tuple[dict[str, dict], bool]:
    """
    Classify AtCoder submissions for a single user and contest by comparing
    submission timestamps to the original contest window.

    Returns:
        per_problem: {problem_id: {solved, solve_type, attempts, wrong_verdicts}}
        did_participate: user had submissions during the contest window or was rated
    """
    relevant = sorted(
        [s for s in subs if s.get("problem_id") in problem_ids],
        key=lambda s: s.get("epoch_second", 0),
    )

    did_live = any(
        contest_start <= s.get("epoch_second", 0) < contest_end
        for s in relevant
    )
    did_participate = did_live or had_rated

    by_problem: dict[str, list[dict]] = defaultdict(list)
    for s in relevant:
        by_problem[s["problem_id"]].append(s)

    per_problem: dict[str, dict] = {}
    for pid, psubs in by_problem.items():
        live_subs = [s for s in psubs if contest_start <= s.get("epoch_second", 0) < contest_end]
        post_subs = [s for s in psubs if s.get("epoch_second", 0) >= contest_end]

        live_ac = next((s for s in live_subs if s.get("result") == "AC"), None)
        post_ac = next((s for s in post_subs if s.get("result") == "AC"), None)
        # Tasks reused by a later contest (an ADT) were often solved long before it started.
        pre_ac = next((s for s in psubs if s.get("epoch_second", 0) < contest_start and s.get("result") == "AC"), None)

        if live_ac:
            wrong_before = [
                s.get("result", "") for s in live_subs
                if s.get("epoch_second", 0) < live_ac["epoch_second"]
                and s.get("result") not in ("AC", None, "")
            ]
            per_problem[pid] = {
                "solved": True,
                "solve_type": "live",
                "attempts": len(wrong_before),
                "wrong_verdicts": list(dict.fromkeys(wrong_before)),
            }
        elif post_ac:
            all_wrong_before = [
                s.get("result", "") for s in psubs
                if s.get("epoch_second", 0) < post_ac["epoch_second"]
                and s.get("result") not in ("AC", None, "")
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
            all_wrong = [
                s.get("result", "") for s in psubs
                if s.get("result") not in ("AC", None, "")
            ]
            per_problem[pid] = {
                "solved": False,
                "solve_type": None,
                "attempts": 0,
                "wrong_verdicts": list(dict.fromkeys(all_wrong)),
            }

    return per_problem, did_participate
