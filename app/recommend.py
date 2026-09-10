"""
Contest recommendation engine.

Manages a local DB cache of CF contest metadata + problem ratings.
The cache is filled gradually by the scheduler (20 contests / 15 min).
The recommendation page queries only the DB — no live API calls at render time.
"""
import logging
import re

from sqlalchemy.orm import Session

from app import models
from app.scraper import codeforces as cf

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Division config
# ---------------------------------------------------------------------------

DIVISION_LABELS: dict[str, str] = {
    "div1":       "Div. 1",
    "div2":       "Div. 2",
    "div3":       "Div. 3",
    "div4":       "Div. 4",
    "educational":"Educational",
    "global":     "Global Round",
    "combined":   "Div. 1 + Div. 2",
    "other":      "Other",
}

# Typical problem rating distributions per division — used as a fallback
# when a contest's problem ratings haven't been fetched yet.
DIVISION_TYPICAL: dict[str, list[int]] = {
    "div4":        [800,  900,  1000, 1100, 1200, 1400],
    "div3":        [800,  1000, 1200, 1400, 1600, 1800, 2000],
    "div2":        [800,  1000, 1300, 1600, 2000, 2400],
    "div1":        [2000, 2200, 2500, 2800, 3000, 3500],
    "educational": [800,  1000, 1200, 1600, 2000, 2400, 2800],
    "global":      [800,  1100, 1400, 1700, 2200, 2700, 3200, 3500],
    "combined":    [1600, 1800, 2000, 2400, 2800, 3200, 3500],
    "other":       [800,  1200, 1600, 2000, 2400, 2800],
}

# How many recent rated CF contests to keep in the cache
CACHE_SIZE = 300

# Guard against concurrent bootstrap (set on first trigger, cleared never — per process)
_bootstrap_running = False


# ---------------------------------------------------------------------------
# Division detection
# ---------------------------------------------------------------------------

def detect_division(name: str) -> str:
    n = name.lower()
    if re.search(r'div\.?\s*1\s*[+&]\s*div\.?\s*2', n):
        return "combined"
    if re.search(r'div\.?\s*4', n):
        return "div4"
    if re.search(r'div\.?\s*3', n):
        return "div3"
    if re.search(r'div\.?\s*2', n):
        return "div2"
    if re.search(r'div\.?\s*1', n):
        return "div1"
    if "educational" in n:
        return "educational"
    if "global" in n:
        return "global"
    return "other"


# ---------------------------------------------------------------------------
# Grade computation
# ---------------------------------------------------------------------------

def _problem_fit(rating: int | None, user_rating: int) -> float:
    """
    How well a single problem at `rating` fits a user at `user_rating`.
    Returns 0.0–1.0.

    Based on Um_nik's "interesting interval" model: problems slightly above
    your rating are where genuine learning happens. Problems below provide
    warmup/speed practice but diminishing learning value.

    Ideal    (diff   0 … +300) → 1.0  (your learning zone — challenging but reachable)
    Stretch  (diff +300 … +500) → 0.6  (hard, but upsolving is valuable)
    Warmup   (diff −200 …   0) → 0.5  (slightly easy — confidence and speed)
    Easy     (diff −400 … −200) → 0.2  (minimal learning, warmup only)
    Very hard (diff +500 … +800) → 0.1  (editorial value only)
    Otherwise                   → 0.0  (trivial or completely unreachable)
    """
    if rating is None:
        return 0.5  # unknown rating: neutral contribution
    diff = rating - user_rating
    if 0 <= diff <= 300:    return 1.0
    if 300 < diff <= 500:   return 0.6
    if -200 <= diff < 0:    return 0.5
    if -400 <= diff < -200: return 0.2
    if 500 < diff <= 800:   return 0.1
    return 0.0


def grade_contest(ratings: list[int | None], user_rating: int) -> dict:
    """
    Compute a fit grade for a contest given its problem ratings.

    Uses a geometric-weighted average (best-fit problem carries ~50% of the
    score, 2nd-best ~25%, etc.) instead of a straight mean. This matches the
    reality that a CF contest typically has only 1-2 problems in a person's
    "interesting interval" — those problems determine the contest's value, and
    averaging them with 4 easy/impossible problems would dilute the signal.

    Returns:
      score   – float 0–10
      letter  – "A" / "B" / "C" / "D"
      color   – CSS color for the badge
      in_zone – count of problems in the ideal learning zone
      total   – total problem count
    """
    if not ratings:
        return {"score": 5.0, "letter": "?", "color": "#888", "in_zone": 0, "total": 0}

    fits = sorted([_problem_fit(r, user_rating) for r in ratings], reverse=True)
    n = len(fits)

    weights = [1.0 / (2 ** i) for i in range(n)]
    total_w = sum(weights)
    weighted_avg = sum(f * w for f, w in zip(fits, weights)) / total_w
    score = round(weighted_avg * 10, 1)

    in_zone = sum(1 for r in ratings if r is not None and 0 <= r - user_rating <= 300)
    stretch = sum(1 for r in ratings if r is not None and 300 < r - user_rating <= 500)

    if score >= 8.0:
        letter, color = "A", "#2e7d32"
    elif score >= 6.0:
        letter, color = "B", "#1565c0"
    elif score >= 3.5:
        letter, color = "C", "#e65100"
    else:
        letter, color = "D", "#b71c1c"

    return {
        "score": score,
        "letter": letter,
        "color": color,
        "in_zone": in_zone,
        "stretch": stretch,
        "total": len(ratings),
    }


# ---------------------------------------------------------------------------
# Cache management  (called by scheduler and on first page visit)
# ---------------------------------------------------------------------------

async def refresh_contest_metadata(db: Session) -> int:
    """
    Fetch contest.list from CF API and upsert the most recent CACHE_SIZE
    rated (type=CF) finished contests. Returns count of new rows added.

    Cost: 1 CF API call (rate-limited 2.1 s slot consumed).
    """
    try:
        all_contests = await cf.get_contest_list()
    except Exception as exc:
        logger.warning("Failed to fetch CF contest list: %s", exc)
        return 0

    rated = [
        c for c in all_contests
        if c.get("type") == "CF" and c.get("phase") == "FINISHED"
    ]
    rated.sort(key=lambda c: c.get("startTimeSeconds", 0), reverse=True)

    added = 0
    for c in rated[:CACHE_SIZE]:
        if db.get(models.CfContest, c["id"]) is None:
            db.add(models.CfContest(
                id=c["id"],
                name=c["name"],
                start_time=c.get("startTimeSeconds"),
                duration_seconds=c.get("durationSeconds"),
                division=detect_division(c["name"]),
            ))
            added += 1
    db.commit()
    logger.info("Contest metadata refresh: %d new contest(s) added", added)
    return added


async def prefetch_contest_problems(db: Session, batch_size: int = 1) -> int:
    """
    Fetch and cache problem ratings for up to `batch_size` contests that
    have not been fetched yet (most recent first).

    Each contest costs one CF API call (2.1 s rate-limited slot).
    batch_size=20 → ~42 s wall time.

    Returns count of contests successfully fetched.
    """
    uncached = (
        db.query(models.CfContest)
        .filter(models.CfContest.problems_fetched == False)   # noqa: E712
        .order_by(models.CfContest.start_time.desc())
        .limit(batch_size)
        .all()
    )
    contest_ids = [c.id for c in uncached]

    fetched = 0
    for cid in contest_ids:
        try:
            info = await cf.get_contest_info(str(cid))
            problems = info.get("problems", [])
            logger.info("Contest %d: got %d problems from CF API", cid, len(problems))
            for p in problems:
                db.add(models.CfContestProblem(
                    contest_id=cid,
                    index=p.get("index", ""),
                    rating=p.get("rating"),
                ))
            contest = db.get(models.CfContest, cid)
            if contest:
                contest.problems_fetched = True
            db.commit()
            fetched += 1
        except Exception as exc:
            logger.error("Failed to cache CF contest %d: %s", cid, exc, exc_info=True)
            db.rollback()

    logger.info("Contest problem prefetch: %d/%d fetched", fetched, len(contest_ids))
    return fetched


async def bootstrap_cache() -> None:
    """
    One-shot: refresh metadata then fetch first batch of problem ratings.
    Uses its own DB session — safe to run as a BackgroundTask.
    Guarded by _bootstrap_running so concurrent page hits don't double-trigger.
    """
    global _bootstrap_running
    if _bootstrap_running:
        return
    _bootstrap_running = True
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        await refresh_contest_metadata(db)
        await prefetch_contest_problems(db, batch_size=5)
    finally:
        db.close()
        _bootstrap_running = False


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

def get_cache_progress(db: Session) -> dict:
    """Return {total, fetched, pct} for the progress bar."""
    total = db.query(models.CfContest).count()
    fetched = (
        db.query(models.CfContest)
        .filter(models.CfContest.problems_fetched == True)   # noqa: E712
        .count()
    )
    pct = int(fetched / total * 100) if total else 0
    return {"total": total, "fetched": fetched, "pct": pct}


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

def get_recommendations(
    db: Session,
    divisions: list[str],
    user_rating: int,
    top_n: int = 5,
) -> list[dict]:
    """
    Return the top_n most suitable CF contests for a user at user_rating,
    filtered by divisions.

    Contests with cached problem ratings are graded exactly; others fall back
    to the typical rating distribution for their division.

    Sort order: grade score DESC, then recency DESC.
    """
    contests = (
        db.query(models.CfContest)
        .filter(models.CfContest.division.in_(divisions))
        .order_by(models.CfContest.start_time.desc())
        .limit(CACHE_SIZE)
        .all()
    )

    scored = []
    for contest in contests:
        if contest.problems_fetched and contest.problems:
            ratings = [p.rating for p in contest.problems]
            exact = True
        else:
            ratings = DIVISION_TYPICAL.get(contest.division, DIVISION_TYPICAL["other"])
            exact = False

        g = grade_contest(ratings, user_rating)
        g["exact"] = exact
        scored.append({"contest": contest, "grade": g})

    scored.sort(key=lambda x: (-x["grade"]["score"], -(x["contest"].start_time or 0)))
    return scored[:top_n]
