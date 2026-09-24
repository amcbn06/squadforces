"""The "Recent submissions" box on the profile page."""
import re
import unittest
from datetime import datetime, timezone
from unittest import mock

from fastapi.testclient import TestClient

from app import histories, models, submissions
from app.platforms.base import KnownState, SubmissionData
from app.database import SessionLocal
from app.main import app
from app.platforms import registry
from app.scraper import kilonova as kn_api
from tests.support import DbTestCase, reset_db


class FakeKilonovaProblems:
    """Patches kilonova.get_problem; `names` maps id -> title, unknown ids fail like a 404."""

    def __init__(self, testcase, names=None):
        self.names = names or {4373: "Bine-i sade mesei mele", 77: "Some Problem"}
        self.calls = []

        async def get_problem(pid):
            self.calls.append(pid)
            if pid not in self.names:
                raise ValueError("Kilonova API error: not found")
            return {"id": pid, "name": self.names[pid], "score_scale": 100}

        p = mock.patch.object(kn_api, "get_problem", get_problem)
        p.start()
        testcase.addCleanup(p.stop)


def sub(user_id, platform, sid, problem, at, **kw):
    fields = dict(user_id=user_id, platform=platform, submission_id=sid, problem_key=problem, submitted_at=at,
                  verdict="AC", accepted=True, final=True)
    fields.update(kw)
    return models.Submission(**fields)


class SubmissionLinks(unittest.TestCase):
    def s(self, **kw):
        return models.Submission(**{"submission_id": 99, "problem_key": "1/A", "submitted_at": 1, **kw})

    def test_codeforces(self):
        cf = registry.get("codeforces")
        regular = self.s(contest_key="2000", problem_key="2000/B", problem_name="Beta")
        self.assertEqual(cf.submission_url(regular), "https://codeforces.com/contest/2000/submission/99")
        self.assertEqual(cf.submission_problem_url(regular), "https://codeforces.com/problemset/problem/2000/B")
        self.assertEqual(cf.submission_problem_label(regular), "2000B. Beta")
        gym = self.s(contest_key="105427", problem_key="105427/K", problem_name="Karl Coder")
        self.assertEqual(cf.submission_url(gym), "https://codeforces.com/gym/105427/submission/99")
        self.assertEqual(cf.submission_problem_url(gym), "https://codeforces.com/gym/105427/problem/K")

    def test_atcoder(self):
        at = registry.get("atcoder")
        s = self.s(contest_key="adt_easy_20240101_1", problem_key="abc343_f")
        self.assertEqual(at.submission_url(s), "https://atcoder.jp/contests/adt_easy_20240101_1/submissions/99")
        self.assertEqual(at.submission_problem_url(s), "https://atcoder.jp/contests/adt_easy_20240101_1/tasks/abc343_f")
        self.assertEqual(at.submission_problem_label(s), "abc343_f")

    def test_kilonova(self):
        kn = registry.get("kilonova")
        s = self.s(problem_key="4373")
        self.assertEqual(kn.submission_url(s), "https://kilonova.ro/submissions/99")
        self.assertEqual(kn.submission_problem_url(s), "https://kilonova.ro/problems/4373")
        self.assertEqual(kn.submission_problem_label(s), "Problem 4373")
        s.problem_name = "Bine-i sade mesei mele"
        self.assertEqual(kn.submission_problem_label(s), "Problem 4373: Bine-i sade mesei mele")

    def test_platforms_without_submissions_have_no_links(self):
        self.assertEqual(registry.get("cses").submission_url(self.s()), "")


class RecentList(DbTestCase):
    async def test_newest_first_across_platforms_and_limited(self):
        u, other = self.user("a", cf="x"), self.user("b")
        rows = [sub(u.id, "codeforces", i, f"{i}/A", 1000 + i * 10) for i in range(1, 16)]
        rows += [sub(u.id, "atcoder", 100 + i, f"abc{i}_a", 1005 + i * 10, contest_key=f"abc{i}") for i in range(1, 11)]
        rows.append(sub(other.id, "codeforces", 500, "777/Z", 99999))  # someone else's
        self.db.add_all(rows)
        self.db.commit()
        out = await histories.recent_submissions(self.db, u, 20)
        self.assertEqual(len(out), 20)
        times = [r["at"] for r in out]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual({r["platform"].key for r in out}, {"codeforces", "atcoder"})
        self.assertNotIn("777/Z", [r["problem"] for r in out])
        self.assertEqual(out[0]["at"], datetime.fromtimestamp(1150, timezone.utc))

    async def test_display_fields(self):
        FakeKilonovaProblems(self)
        u = self.user("a", cf="x")
        self.db.add_all([
            sub(u.id, "codeforces", 1, "105427/A", 100, contest_key="105427", problem_name="Aperiodic",
                participant_type="VIRTUAL", team_name="[UAIC] team"),
            sub(u.id, "codeforces", 2, "1/A", 200, verdict="TES", accepted=False, final=False),
            sub(u.id, "kilonova", 3, "77", 300, verdict="PT", accepted=False, score=60.0, max_score=100.0),
            sub(u.id, "atcoder", 4, "abc1_a", 400, verdict="WA", accepted=False, contest_key="abc1"),
        ])
        self.db.commit()
        atc, kn, pending, gym = await histories.recent_submissions(self.db, u)
        self.assertEqual((atc["verdict"], atc["accepted"]), ("WA", False))
        self.assertEqual(kn["verdict"], "PT 60/100")
        self.assertEqual(kn["problem"], "Problem 77: Some Problem")
        self.assertTrue(pending["pending"])
        self.assertEqual((gym["mode"], gym["team"], gym["problem"]), ("virtual", "[UAIC] team", "105427A. Aperiodic"))

    async def test_no_submissions(self):
        u = self.user("a")
        self.assertEqual(await histories.recent_submissions(self.db, u), [])


class KilonovaTitles(DbTestCase):
    async def test_titles_are_looked_up_once_and_stored_for_everyone(self):
        fake = FakeKilonovaProblems(self)
        a, b = self.user("a", kn="ka"), self.user("b", kn="kb")
        self.db.add_all([sub(a.id, "kilonova", 1, "4373", 10), sub(a.id, "kilonova", 2, "4373", 20),
                         sub(a.id, "kilonova", 3, "77", 30), sub(b.id, "kilonova", 9, "4373", 40)])
        self.db.commit()
        rows = await histories.recent_submissions(self.db, a)
        self.assertEqual([r["problem"] for r in rows], ["Problem 77: Some Problem", "Problem 4373: Bine-i sade mesei mele",
                                                        "Problem 4373: Bine-i sade mesei mele"])
        self.assertEqual(sorted(fake.calls), [77, 4373])           # one lookup per problem, not per submission
        self.assertEqual(self.db.query(models.Submission).filter_by(problem_key="4373", problem_name=None).count(), 0)
        fake.calls.clear()
        await histories.recent_submissions(self.db, a)
        rows_b = await histories.recent_submissions(self.db, b)      # another user's row was named by the first lookup
        self.assertEqual(fake.calls, [])
        self.assertEqual(rows_b[0]["problem"], "Problem 4373: Bine-i sade mesei mele")

    async def test_a_name_another_user_already_has_needs_no_lookup(self):
        fake = FakeKilonovaProblems(self)
        a, b = self.user("a", kn="ka"), self.user("b", kn="kb")
        self.db.add_all([sub(a.id, "kilonova", 1, "77", 10, problem_name="Some Problem"), sub(b.id, "kilonova", 2, "77", 20)])
        self.db.commit()
        rows = await histories.recent_submissions(self.db, b)
        self.assertEqual((rows[0]["problem"], fake.calls), ("Problem 77: Some Problem", []))

    async def test_a_failed_lookup_leaves_the_number_and_is_retried_next_time(self):
        fake = FakeKilonovaProblems(self, names={})
        a = self.user("a", kn="ka")
        self.db.add(sub(a.id, "kilonova", 1, "555", 10))
        self.db.commit()
        rows = await histories.recent_submissions(self.db, a)
        self.assertEqual(rows[0]["problem"], "Problem 555")
        fake.names[555] = "Now Exists"
        rows = await histories.recent_submissions(self.db, a)
        self.assertEqual(rows[0]["problem"], "Problem 555: Now Exists")

    async def test_other_platforms_are_not_looked_up(self):
        fake = FakeKilonovaProblems(self)
        a = self.user("a", cf="x")
        self.db.add(sub(a.id, "atcoder", 1, "abc1_a", 10, contest_key="abc1"))
        self.db.commit()
        rows = await histories.recent_submissions(self.db, a)
        self.assertEqual((rows[0]["problem"], fake.calls), ("abc1_a", []))

    async def test_a_refresh_does_not_blank_a_stored_title(self):
        a = self.user("a", kn="ka")
        self.db.add(sub(a.id, "kilonova", 1, "77", 10, problem_name="Some Problem", verdict="WA", accepted=False, final=False))
        self.db.commit()
        # the judge reports the same submission again, now judged, and (as always for Kilonova) without a title
        submissions._store(self.db, a.id, "kilonova", [SubmissionData(
            submission_id=1, problem_key="77", submitted_at=10, verdict="AC", accepted=True, final=True)])
        self.db.commit()
        row = self.db.query(models.Submission).filter_by(user_id=a.id).one()
        self.assertEqual((row.verdict, row.problem_name), ("AC", "Some Problem"))


class ProfilePage(unittest.TestCase):
    def setUp(self):
        reset_db()
        FakeKilonovaProblems(self)
        p = mock.patch("app.scheduler.start")
        p.start()
        self.addCleanup(p.stop)
        ctx = TestClient(app, follow_redirects=False)
        self.admin = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.admin.post("/login", data={"username": "admin", "password": "test-admin-pw"})
        self.admin.post("/admin/users/new", data={"username": "u", "password": "secret123", "user_type": "user"})

    def page(self):
        r = self.admin.get("/users/u")
        self.assertEqual(r.status_code, 200)
        return re.sub(r"\s+", " ", r.text)

    def test_empty_state(self):
        self.assertIn("No submissions stored yet.", self.page())

    def test_rows_with_links_icons_and_verdicts(self):
        db = SessionLocal()
        uid = db.query(models.User).filter_by(username="u").one().id
        db.add_all([
            sub(uid, "codeforces", 555, "105427/K", 1_780_000_000, contest_key="105427", problem_name="Karl Coder",
                participant_type="VIRTUAL"),
            sub(uid, "atcoder", 777, "abc343_f", 1_780_000_100, contest_key="abc343", verdict="TLE", accepted=False),
            sub(uid, "kilonova", 888, "4373", 1_780_000_200, score=100.0, max_score=100.0),
        ])
        db.commit()
        db.close()
        html = self.page()
        for href in ["https://codeforces.com/gym/105427/submission/555", "https://codeforces.com/gym/105427/problem/K",
                     "https://atcoder.jp/contests/abc343/submissions/777", "https://atcoder.jp/contests/abc343/tasks/abc343_f",
                     "https://kilonova.ro/submissions/888", "https://kilonova.ro/problems/4373"]:
            self.assertIn(f'href="{href}"', html)
        for icon in ("/static/cf.png", "/static/ac.png", "/static/kn.png"):
            self.assertIn(icon, html)
        self.assertIn("105427K. Karl Coder", html)
        self.assertIn("(virtual)", html)
        self.assertIn('<span class="wv">TLE</span>', html)
        # newest first: the Kilonova row (latest) comes before the Codeforces one
        self.assertLess(html.index("submissions/888"), html.index("submission/555"))
        self.assertIn("Problem 4373: Bine-i sade mesei mele", html)
        self.assertIn("(last 3, all platforms", html)


if __name__ == "__main__":
    unittest.main()
