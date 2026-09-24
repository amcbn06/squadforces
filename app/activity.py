"""Daily submission counts for the profile heatmap, read from the local submission store."""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app import submissions
from app.platforms import registry

logger = logging.getLogger(__name__)

# Platform key -> the short name the heatmap script uses for it
HEATMAP_PLATFORMS = {"codeforces": "cf", "atcoder": "atc"}
ACTIVITY_MAX_AGE = timedelta(hours=1)  # how stale a stored copy may be before a page view refreshes it


async def user_activity(db: Session, user) -> dict[str, dict[str, int]]:
    """{"YYYY-MM-DD": {"cf": n, "atc": n}} for the last two calendar years."""
    now = datetime.now(timezone.utc)
    since = datetime(now.year - 2, 1, 1, tzinfo=timezone.utc).timestamp()
    activity: dict[str, dict[str, int]] = {}

    for key, short in HEATMAP_PLATFORMS.items():
        platform = registry.get(key)
        if not platform.handle_of(user):
            continue
        try:
            await submissions.refresh_user(db, user, platform, max_age=ACTIVITY_MAX_AGE)
        except Exception:
            logger.warning("Could not refresh %s submissions of %s for the heatmap", key, user.username, exc_info=True)
        for day, count in submissions.daily_counts(db, user.id, key, since).items():
            activity.setdefault(day, {"cf": 0, "atc": 0})[short] += count
    return activity
