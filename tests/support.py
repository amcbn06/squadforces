"""Shared helpers: a clean database per test, object factories, and builders for API-shaped submissions."""
import unittest
from datetime import datetime

from app import models
from app.database import Base, SessionLocal, engine


def reset_db() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


class DbTestCase(unittest.IsolatedAsyncioTestCase):
    """Fresh tables and one session per test."""

    def setUp(self):
        reset_db()
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    # factories -----------------------------------------------------------------

    def user(self, name="alice", *, cf=None, ac=None, kn=None, user_type="user"):
        u = models.User(username=name, password_hash="x", user_type=user_type,
                        codeforces_handle=cf, atcoder_handle=ac, kilonova_handle=kn)
        self.db.add(u)
        self.db.commit()
        return u

    def group(self, users, name="G"):
        g = models.Group(name=name)
        self.db.add(g)
        self.db.flush()
        for u in users:
            self.db.add(models.GroupMembership(group_id=g.id, user_id=u.id))
        a = models.Assignment(group_id=g.id, title="A")
        self.db.add(a)
        self.db.commit()
        return g, a

    def item(self, assignment, platform, type_, external_id, *, title=None, source_url=None):
        it = models.AssignmentItem(
            assignment_id=assignment.id, type=type_, platform=platform, external_id=external_id,
            title=title, source_url=source_url, sync_status="pending",
        )
        self.db.add(it)
        self.db.commit()
        return it


# Codeforces user.status entries ------------------------------------------------

def cf_raw(sid, contest, index, verdict="OK", *, at=1_700_000_000, ptype="PRACTICE", rel=None,
           name=None, rating=None, team=None, contest_of_sub=None):
    """One entry as returned by user.status."""
    author = {"contestId": contest, "participantType": ptype, "members": [{"handle": "x"}]}
    if team:
        author["teamId"], author["teamName"] = team
    return {
        "id": sid,
        "contestId": contest if contest_of_sub is None else contest_of_sub,
        "creationTimeSeconds": at,
        "relativeTimeSeconds": rel if rel is not None else 2147483647,
        "problem": {"contestId": contest, "index": index, "name": name or f"P{index}",
                    **({"rating": rating} if rating else {})},
        "author": author,
        "programmingLanguage": "C++20",
        "verdict": verdict,
    }


def ac_raw(sid, problem, result="AC", *, at=1_700_000_000, contest=None, point=100.0):
    return {"id": sid, "epoch_second": at, "problem_id": problem, "contest_id": contest or problem.split("_")[0],
            "user_id": "u", "language": "C++", "point": point, "length": 1, "result": result, "execution_time": 1}


def kn_raw(sid, problem, score, *, scale=100, at="2026-05-09T13:37:53.160257+02:00", status="finished",
           compile_error=False, contest=None, icpc=None):
    return {"id": sid, "created_at": at, "user_id": 1, "problem_id": problem, "language": "cpp17",
            "status": status, "compile_error": compile_error, "contest_id": contest, "score": score,
            "score_scale": scale, "icpc_verdict": icpc, "submission_type": "classic"}
