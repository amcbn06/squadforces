from datetime import datetime
from sqlalchemy import (
    BigInteger, Column, Float, Index, Integer, String, Boolean, DateTime, Date,
    Text, JSON, ForeignKey, UniqueConstraint
)
from sqlalchemy import false, text as sql_text
from sqlalchemy.orm import relationship
from app.database import Base


class User(Base):
    """Single auth + profile entity. id=0 is the admin."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(200), nullable=False)
    # Bumped whenever the password changes; a session is only valid while its stored copy matches.
    session_version = Column(Integer, nullable=False, default=0, server_default="0")
    user_type = Column(String(20), nullable=False, default="user")  # admin|user|student
    full_name = Column(String(100), nullable=True)
    codeforces_handle = Column(String(50), nullable=True)
    atcoder_handle = Column(String(50), nullable=True)
    kilonova_handle = Column(String(50), nullable=True)
    cf_rating = Column(Integer, nullable=True)
    cf_rank = Column(String(30), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    memberships = relationship("GroupMembership", back_populates="user", cascade="all, delete-orphan")
    results = relationship("Result", back_populates="user", cascade="all, delete-orphan")


class Group(Base):
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    hints_allowed = Column(Boolean, nullable=False, default=False, server_default=false())
    # Who manages the group (remove members, write hints, invite, delete). The admin (id 0) for groups made before
    # ownership existed; NULL is treated the same way. Admin can always manage any group.
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    max_members = Column(Integer, nullable=True)  # joining through an invite stops at this size; admin can exceed it
    created_at = Column(DateTime, default=datetime.utcnow)

    memberships = relationship("GroupMembership", back_populates="group", cascade="all, delete-orphan")
    owner = relationship("User", foreign_keys=[owner_id])
    invites = relationship("Invite", back_populates="group", cascade="all, delete-orphan")
    assignments = relationship("Assignment", back_populates="group", cascade="all, delete-orphan")


class Invite(Base):
    """A link that lets someone in: create an account (platform invite, from the admin) and/or join a group.
    Only the SHA-256 of the token is stored; the link is shown once when it is created."""
    __tablename__ = "invites"

    id = Column(Integer, primary_key=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    token_hint = Column(String(8), nullable=False, default="")  # last characters of the token, to tell links apart
    label = Column(String(100), nullable=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=True, index=True)  # None = platform
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    user_type = Column(String(20), nullable=False, default="user")  # type of an account created through it
    allows_signup = Column(Boolean, nullable=False, default=False)  # may create an account (else: existing users only)
    max_uses = Column(Integer, nullable=False, default=1)
    uses = Column(Integer, nullable=False, default=0)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    group = relationship("Group", back_populates="invites")
    created_by = relationship("User", foreign_keys=[created_by_id])


class GroupMembership(Base):
    __tablename__ = "group_memberships"
    __table_args__ = (UniqueConstraint("group_id", "user_id"),)

    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    joined_at = Column(DateTime, default=datetime.utcnow)

    group = relationship("Group", back_populates="memberships")
    user = relationship("User", back_populates="memberships")


class Assignment(Base):
    __tablename__ = "assignments"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(200), nullable=False)
    week_start_date = Column(Date, nullable=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    group = relationship("Group", back_populates="assignments")
    items = relationship("AssignmentItem", back_populates="assignment", cascade="all, delete-orphan")


class AssignmentItem(Base):
    __tablename__ = "assignment_items"

    id = Column(Integer, primary_key=True, index=True)
    assignment_id = Column(Integer, ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(10), nullable=False)      # "contest" | "problem"
    platform = Column(String(15), nullable=False)  # a key of app/platforms/registry.py: codeforces | atcoder | kilonova | cses | other
    external_id = Column(String(100), nullable=False)
    title = Column(String(300), nullable=True)
    rating = Column(Integer, nullable=True)  # standalone Codeforces problems only
    source_url = Column(String(500), nullable=True)  # original link, kept where it can't be rebuilt from the ID (CF EDU)
    added_at = Column(DateTime, default=datetime.utcnow)
    last_synced_at = Column(DateTime, nullable=True)
    sync_status = Column(String(20), default="pending")  # pending|syncing|done|error
    sync_error = Column(Text, nullable=True)
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    assignment = relationship("Assignment", back_populates="items")
    contest_problems = relationship("ContestProblem", back_populates="assignment_item", cascade="all, delete-orphan")
    results = relationship("Result", back_populates="assignment_item", cascade="all, delete-orphan")
    hints = relationship("Hint", back_populates="assignment_item", cascade="all, delete-orphan")

    # What differs per platform (manual marking, default title) is answered by the platform module.
    @property
    def manual_status(self) -> bool:
        """True where solve status can't be fetched (CSES, Codeforces EDU, links to other sites), so members
        mark it themselves and the title is entered by hand."""
        from app.platforms import registry
        return registry.for_item(self).manual_status(self)

    @property
    def display_title(self) -> str:
        from app.platforms import registry
        return self.title or registry.for_item(self).default_title(self)


class ContestProblem(Base):
    __tablename__ = "contest_problems"

    id = Column(Integer, primary_key=True, index=True)
    assignment_item_id = Column(Integer, ForeignKey("assignment_items.id", ondelete="CASCADE"), nullable=False)
    platform_problem_id = Column(String(50), nullable=False)
    index = Column(String(10), nullable=False)
    name = Column(String(300), nullable=False)
    rating = Column(Integer, nullable=True)
    max_score = Column(Integer, nullable=True)  # points for a full solve, where the judge scores partially (Kilonova)

    assignment_item = relationship("AssignmentItem", back_populates="contest_problems")
    problem_results = relationship("ProblemResult", back_populates="contest_problem", cascade="all, delete-orphan")
    hints = relationship("Hint", back_populates="contest_problem", cascade="all, delete-orphan")


class Hint(Base):
    """An entry on one problem: a hint or solution (admin-only) or a note (any group member)."""
    __tablename__ = "hints"

    id = Column(Integer, primary_key=True)
    assignment_item_id = Column(Integer, ForeignKey("assignment_items.id", ondelete="CASCADE"), nullable=True, index=True)
    contest_problem_id = Column(Integer, ForeignKey("contest_problems.id", ondelete="CASCADE"), nullable=True, index=True)
    text = Column(Text, nullable=False)
    kind = Column(String(10), nullable=False, default="hint", server_default=sql_text("'hint'"))  # hint|solution|note
    author_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)  # set for notes only
    time_minutes = Column(Integer, nullable=True)  # self-reported time to solve; notes only
    created_at = Column(DateTime, default=datetime.utcnow)

    assignment_item = relationship("AssignmentItem", back_populates="hints")
    contest_problem = relationship("ContestProblem", back_populates="hints")
    author = relationship("User")


class Result(Base):
    __tablename__ = "results"
    __table_args__ = (UniqueConstraint("assignment_item_id", "user_id"),)

    id = Column(Integer, primary_key=True, index=True)
    assignment_item_id = Column(Integer, ForeignKey("assignment_items.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    solved = Column(Boolean, nullable=True)
    solve_time = Column(DateTime, nullable=True)
    rank = Column(Integer, nullable=True)
    old_rating = Column(Integer, nullable=True)
    new_rating = Column(Integer, nullable=True)
    rating_change = Column(Integer, nullable=True)
    problems_solved_count = Column(Integer, nullable=True)
    problems_total_count = Column(Integer, nullable=True)
    participated = Column(Boolean, nullable=True)
    last_synced_at = Column(DateTime, nullable=True)
    raw_scrape_data = Column(JSON, nullable=True)

    assignment_item = relationship("AssignmentItem", back_populates="results")
    user = relationship("User", back_populates="results")


class Submission(Base):
    """One submission a user ever sent to a judge, mirrored locally so any problem or contest status can be
    answered from the database. Rows are only ever added or refreshed by app/submissions.py, never per item."""
    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint("user_id", "platform", "submission_id"),
        Index("ix_submissions_problem", "user_id", "platform", "problem_key"),
        Index("ix_submissions_contest", "user_id", "platform", "contest_key"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    platform = Column(String(15), nullable=False)      # platform key: codeforces | atcoder | kilonova
    submission_id = Column(BigInteger, nullable=False)  # the judge's own id; increases with time
    problem_key = Column(String(120), nullable=False)   # platform-local problem id: "1234/A", "abc343_f", "4373"
    contest_key = Column(String(100), nullable=True)    # contest the submission was sent in, if any
    problem_index = Column(String(20), nullable=True)   # letter within the contest (Codeforces)
    problem_name = Column(String(300), nullable=True)
    problem_rating = Column(Integer, nullable=True)
    verdict = Column(String(12), nullable=False, default="")  # short code shown in the UI: AC, WA, TLE, RE, PT ...
    accepted = Column(Boolean, nullable=False, default=False)
    final = Column(Boolean, nullable=False, default=True)     # False while the judge is still working on it
    score = Column(Float, nullable=True)                # AtCoder points / Kilonova score
    max_score = Column(Float, nullable=True)            # Kilonova score scale (a full solve reaches it)
    submitted_at = Column(BigInteger, nullable=False)   # unix seconds
    relative_seconds = Column(Integer, nullable=True)   # seconds since the contest started (Codeforces)
    participant_type = Column(String(20), nullable=True)  # CONTESTANT | VIRTUAL | PRACTICE | OUT_OF_COMPETITION ...
    team_id = Column(String(20), nullable=True)         # set when sent as part of a team
    team_name = Column(String(120), nullable=True)
    language = Column(String(60), nullable=True)


class SubmissionSync(Base):
    """When a user's submissions on one platform were last refreshed, and how far the local copy reaches."""
    __tablename__ = "submission_syncs"
    __table_args__ = (UniqueConstraint("user_id", "platform"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    platform = Column(String(15), nullable=False)
    handle = Column(String(50), nullable=True)           # handle the stored rows belong to; a change resets them
    last_synced_at = Column(DateTime, nullable=True)     # last successful refresh
    last_attempt_at = Column(DateTime, nullable=True)
    full_sync_at = Column(DateTime, nullable=True)       # when the complete history was first loaded
    newest_submission_id = Column(BigInteger, nullable=True)
    newest_submitted_at = Column(BigInteger, nullable=True)
    submission_count = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)


class RatingEntry(Base):
    """One rated contest in a user's history (Codeforces user.rating, AtCoder contest_history)."""
    __tablename__ = "rating_entries"
    __table_args__ = (UniqueConstraint("user_id", "platform", "contest_key"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    platform = Column(String(15), nullable=False)
    contest_key = Column(String(100), nullable=False)
    contest_name = Column(String(300), nullable=True)
    rank = Column(Integer, nullable=True)
    old_rating = Column(Integer, nullable=True)
    new_rating = Column(Integer, nullable=True)
    performance = Column(Integer, nullable=True)
    rated_at = Column(BigInteger, nullable=True)


class CfContest(Base):
    __tablename__ = "cf_contests"

    id = Column(Integer, primary_key=True)
    name = Column(String(300), nullable=False)
    start_time = Column(Integer, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    division = Column(String(20), nullable=True)
    problems_fetched = Column(Boolean, default=False, nullable=False)

    problems = relationship("CfContestProblem", back_populates="contest", cascade="all, delete-orphan")


class CfContestProblem(Base):
    __tablename__ = "cf_contest_problems"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contest_id = Column(Integer, ForeignKey("cf_contests.id", ondelete="CASCADE"), nullable=False)
    index = Column(String(5), nullable=False)
    rating = Column(Integer, nullable=True)

    contest = relationship("CfContest", back_populates="problems")


class ProblemResult(Base):
    __tablename__ = "problem_results"
    __table_args__ = (UniqueConstraint("contest_problem_id", "user_id"),)

    id = Column(Integer, primary_key=True, index=True)
    contest_problem_id = Column(Integer, ForeignKey("contest_problems.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    solved = Column(Boolean, default=False)
    penalty = Column(Integer, nullable=True)
    solve_type = Column(String(20), nullable=True)
    attempts = Column(Integer, nullable=True)
    best_wrong_verdict = Column(String(30), nullable=True)
    score = Column(Integer, nullable=True)

    contest_problem = relationship("ContestProblem", back_populates="problem_results")
    user = relationship("User")
