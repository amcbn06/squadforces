from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Date,
    Text, JSON, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import relationship
from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    display_name = Column(String(100), nullable=False)
    codeforces_handle = Column(String(50), unique=True, nullable=False, index=True)
    atcoder_handle = Column(String(50), unique=True, nullable=True)
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
    created_at = Column(DateTime, default=datetime.utcnow)

    memberships = relationship("GroupMembership", back_populates="group", cascade="all, delete-orphan")
    assignments = relationship("Assignment", back_populates="group", cascade="all, delete-orphan")


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
    platform = Column(String(15), nullable=False)  # "codeforces" | "atcoder"
    external_id = Column(String(100), nullable=False)
    title = Column(String(300), nullable=True)      # populated by scraping
    added_at = Column(DateTime, default=datetime.utcnow)
    last_synced_at = Column(DateTime, nullable=True)
    sync_status = Column(String(20), default="pending")  # pending|syncing|done|error
    sync_error = Column(Text, nullable=True)

    assignment = relationship("Assignment", back_populates="items")
    contest_problems = relationship("ContestProblem", back_populates="assignment_item", cascade="all, delete-orphan")
    results = relationship("Result", back_populates="assignment_item", cascade="all, delete-orphan")


class ContestProblem(Base):
    __tablename__ = "contest_problems"

    id = Column(Integer, primary_key=True, index=True)
    assignment_item_id = Column(Integer, ForeignKey("assignment_items.id", ondelete="CASCADE"), nullable=False)
    platform_problem_id = Column(String(50), nullable=False)
    index = Column(String(10), nullable=False)  # A, B, C...
    name = Column(String(300), nullable=False)
    rating = Column(Integer, nullable=True)

    assignment_item = relationship("AssignmentItem", back_populates="contest_problems")
    problem_results = relationship("ProblemResult", back_populates="contest_problem", cascade="all, delete-orphan")


class Result(Base):
    """Result for a user on an AssignmentItem (contest or standalone problem)."""
    __tablename__ = "results"
    __table_args__ = (UniqueConstraint("assignment_item_id", "user_id"),)

    id = Column(Integer, primary_key=True, index=True)
    assignment_item_id = Column(Integer, ForeignKey("assignment_items.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    # For standalone problems
    solved = Column(Boolean, nullable=True)
    solve_time = Column(DateTime, nullable=True)

    # For contests
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


class CfContest(Base):
    """Cached metadata for a CF rated contest (used by the recommendation engine)."""
    __tablename__ = "cf_contests"

    id = Column(Integer, primary_key=True)  # CF contest id (not autoincrement)
    name = Column(String(300), nullable=False)
    start_time = Column(Integer, nullable=True)       # unix timestamp
    duration_seconds = Column(Integer, nullable=True)
    division = Column(String(20), nullable=True)      # div1/div2/div3/div4/educational/global/combined/other
    problems_fetched = Column(Boolean, default=False, nullable=False)

    problems = relationship("CfContestProblem", back_populates="contest", cascade="all, delete-orphan")


class CfContestProblem(Base):
    """Cached problem rating for one problem in a CfContest."""
    __tablename__ = "cf_contest_problems"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contest_id = Column(Integer, ForeignKey("cf_contests.id", ondelete="CASCADE"), nullable=False)
    index = Column(String(5), nullable=False)   # "A", "B", "C", …
    rating = Column(Integer, nullable=True)     # None for unrated problems

    contest = relationship("CfContest", back_populates="problems")


class ProblemResult(Base):
    """Solved/unsolved per user per problem within a contest."""
    __tablename__ = "problem_results"
    __table_args__ = (UniqueConstraint("contest_problem_id", "user_id"),)

    id = Column(Integer, primary_key=True, index=True)
    contest_problem_id = Column(Integer, ForeignKey("contest_problems.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    solved = Column(Boolean, default=False)
    penalty = Column(Integer, nullable=True)
    # live | virtual | upsolving | standalone
    solve_type = Column(String(20), nullable=True)
    # wrong submissions before first AC (or total if never AC'd)
    attempts = Column(Integer, nullable=True)
    # space-separated short verdicts for unsolved problems, e.g. "WA TLE"
    best_wrong_verdict = Column(String(30), nullable=True)

    contest_problem = relationship("ContestProblem", back_populates="problem_results")
    user = relationship("User")
