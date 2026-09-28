"""Daily solve counts for the profile heatmap, read from the local submission store."""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app import submissions
from app.platforms import registry

logger = logging.getLogger(__name__)

# Platform key -> the short name the heatmap script uses for it
HEATMAP_PLATFORMS = {"codeforces": "cf", "atcoder": "atc", "kilonova": "kn"}
ACTIVITY_MAX_AGE = timedelta(hours=1)  # how stale a stored copy may be before a page view refreshes it


async def user_activity(db: Session, user) -> dict[str, dict[str, int]]:
    """{"YYYY-MM-DD": {"cf": n, "atc": n, "kn": n}} of accepted submissions (solves), from the user's very first
    stored submission on (that start point still looks at every submission, solved or not — the heatmap only
    needs the range to reach back far enough, not an early solve specifically)."""
    now = datetime.now(timezone.utc)
    activity: dict[str, dict[str, int]] = {}
    earliest = None

    for key, short in HEATMAP_PLATFORMS.items():
        platform = registry.get(key)
        if not platform.handle_of(user):
            continue
        try:
            await submissions.refresh_user(db, user, platform, max_age=ACTIVITY_MAX_AGE)
        except Exception:
            logger.warning("Could not refresh %s submissions of %s for the heatmap", key, user.username, exc_info=True)
        first = submissions.earliest_submission_at(db, user.id, key)
        if first is not None:
            earliest = first if earliest is None else min(earliest, first)

    # Back to Jan 1 of the year of the very first submission (any platform), so an older year is never cut
    # off partway; with no history at all there is nothing to show, so this year alone is enough.
    since_year = datetime.fromtimestamp(earliest, tz=timezone.utc).year if earliest is not None else now.year
    since = datetime(since_year, 1, 1, tzinfo=timezone.utc).timestamp()

    for key, short in HEATMAP_PLATFORMS.items():
        platform = registry.get(key)
        if not platform.handle_of(user):
            continue
        for day, count in submissions.daily_solved_counts(db, user.id, key, since).items():
            activity.setdefault(day, {short_name: 0 for short_name in HEATMAP_PLATFORMS.values()})[short] += count
    return activity
