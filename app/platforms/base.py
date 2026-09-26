"""What every judge platform has to provide, and the small data types passed around between layers.

A platform module (cf.py, atc.py, ...) subclasses `Platform` once and registers itself in
`app/platforms/__init__.py`. The rest of the app only ever talks to this interface.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import ParseResult


@dataclass(frozen=True)
class ParsedLink:
    """A contest or problem recognised from a link or ID, ready to become an AssignmentItem."""
    platform: str            # registry key of the owning platform
    type: str                # "problem" | "contest"
    external_id: str         # what the platform's own module uses to look it up
    label: str               # short human description for confirmation messages
    source_url: Optional[str] = None  # original link, where it can't be rebuilt from the ID


@dataclass
class SubmissionData:
    """One submission in the platform-neutral shape stored in the `submissions` table."""
    submission_id: int
    problem_key: str
    submitted_at: int
    verdict: str = ""
    accepted: bool = False
    final: bool = True
    contest_key: Optional[str] = None
    problem_index: Optional[str] = None
    problem_name: Optional[str] = None
    problem_rating: Optional[int] = None
    score: Optional[float] = None
    max_score: Optional[float] = None
    relative_seconds: Optional[int] = None
    participant_type: Optional[str] = None
    team_id: Optional[str] = None
    team_name: Optional[str] = None
    language: Optional[str] = None


@dataclass
class RatingData:
    """One rated contest in a user's history."""
    contest_key: str
    contest_name: Optional[str] = None
    rank: Optional[int] = None
    old_rating: Optional[int] = None
    new_rating: Optional[int] = None
    performance: Optional[int] = None
    rated_at: Optional[int] = None


@dataclass
class KnownState:
    """What the local store already holds for a user, so a refresh only has to fetch what is newer.

    `stop_id` / `stop_at` mark how far back the fetch must reach: the newest stored submission, or the oldest
    one the judge had not finished grading yet (its verdict may have changed since). None means the store is
    empty and the whole history is needed."""
    stop_id: Optional[int] = None
    stop_at: Optional[int] = None


class Platform:
    """Base class. Override what applies; the defaults describe a platform with no API and no contests."""

    key: str = ""                      # stored in AssignmentItem.platform and Submission.platform
    label: str = ""                    # shown to users
    icon: Optional[str] = None         # static file for the platform icon; None shows a "?" placeholder
    handle_attr: Optional[str] = None  # User column holding the handle; None = nothing to fetch per user
    profile_url: Optional[str] = None  # "{handle}" placeholder, e.g. "https://codeforces.com/profile/{handle}"
    supports_contests = False
    has_submissions = False            # True if fetch_submissions() is implemented

    # ── link recognition ─────────────────────────────────────────────────────

    def parse_url(self, source: str, url: ParseResult) -> Optional[ParsedLink]:
        """Turn a recognised URL into a ParsedLink, or None if it isn't a contest/problem page.
        `source` is what problems.detect_source() decided (a platform can own several, e.g. cf / cf_edu / cf_gym)."""
        return None

    def parse_bare(self, token: str) -> Optional[ParsedLink]:
        """A bare ID with no URL (e.g. "1234A"), or None."""
        return None

    def parse_form(self, item_type: str, raw: str) -> tuple[Optional[ParsedLink], Optional[str]]:
        """Interpret what someone typed into the "add item" form for this platform: (link, error message)."""
        return None, f"{self.label} items can't be added this way."

    # ── presentation ─────────────────────────────────────────────────────────

    def item_url(self, item) -> str:
        return item.source_url or ""

    def problem_url(self, item, contest_problem) -> str:
        """Link for one problem inside a contest item."""
        return self.item_url(item)

    def submission_url(self, sub) -> str:
        """Link to one stored submission on the judge ("" if it can't be built)."""
        return ""

    def submission_problem_url(self, sub) -> str:
        """Link to the problem a stored submission was sent to."""
        return ""

    def submission_problem_label(self, sub) -> str:
        """Readable name of the submission's problem."""
        return sub.problem_name or sub.problem_key

    def problem_keys(self, item) -> set[str]:
        """The `Submission.problem_key`s of the problems this item covers (a problem, or a contest's problems)."""
        if item.type == "problem":
            return {item.external_id}
        return {cp.platform_problem_id for cp in item.contest_problems}

    def manual_status(self, item) -> bool:
        """True where solve status can't be fetched, so members mark it themselves and enter the title."""
        return False

    def default_title(self, item) -> str:
        return item.external_id

    def profile_link(self, handle: str) -> Optional[str]:
        return self.profile_url.format(handle=handle) if self.profile_url and handle else None

    # ── data ─────────────────────────────────────────────────────────────────

    def handle_of(self, user) -> Optional[str]:
        return (getattr(user, self.handle_attr, None) or None) if self.handle_attr else None

    async def fetch_submissions(self, handle: str, known: KnownState) -> list[SubmissionData]:
        """Every submission newer than `known` (older ones may come along; storing them is idempotent)."""
        return []

    async def fetch_problem_names(self, keys: list[str]) -> dict[str, str]:
        """Titles for problem keys the submissions didn't name (only for judges whose submissions lack them)."""
        return {}

    async def fetch_rating_history(self, handle: str) -> Optional[list[RatingData]]:
        """The user's rated-contest history, or None if this platform has none."""
        return None

    async def sync_item(self, item, members: list, db) -> None:
        """Fill in the item's metadata (title, problems, rating) and derive each member's results from the
        submission store. Members' stores have already been refreshed when this runs."""
        return None
