"""The "Recent submissions" box on the profile page."""
import re
import unittest
from datetime import datetime, timezone
from unittest import mock

from fastapi.testclient import TestClient

from app import histories, models
from app.database import SessionLocal
from app.main import app
from app.platforms import registry
from tests.support import DbTestCase, reset_db


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

    def test_platforms_without_submissions_have_no_links(self):
        self.assertEqual(registry.get("cses").submission_url(self.s()), "")


class RecentList(DbTestCase):
    def test_newest_first_across_platforms_and_limited(self):
        u, other = self.user("a", cf="x"), self.user("b")
        rows = [sub(u.id, "codeforces", i, f"{i}/A", 1000 + i * 10) for i in range(1, 16)]
        rows += [sub(u.id, "atcoder", 100 + i, f"abc{i}_a", 1005 + i * 10, contest_key=f"abc{i}") for i in range(1, 11)]
        rows.append(sub(other.id, "codeforces", 500, "777/Z", 99999))  # someone else's
        self.db.add_all(rows)
        self.db.commit()
        out = histories.recent_submissions(self.db, u, 20)
        self.assertEqual(len(out), 20)
        times = [r["at"] for r in out]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual({r["platform"].key for r in out}, {"codeforces", "atcoder"})
        self.assertNotIn("777/Z", [r["problem"] for r in out])
        self.assertEqual(out[0]["at"], datetime.fromtimestamp(1150, timezone.utc))

    def test_display_fields(self):
        u = self.user("a", cf="x")
        self.db.add_all([
            sub(u.id, "codeforces", 1, "105427/A", 100, contest_key="105427", problem_name="Aperiodic",
                participant_type="VIRTUAL", team_name="[UAIC] team"),
            sub(u.id, "codeforces", 2, "1/A", 200, verdict="TES", accepted=False, final=False),
            sub(u.id, "kilonova", 3, "77", 300, verdict="PT", accepted=False, score=60.0, max_score=100.0),
            sub(u.id, "atcoder", 4, "abc1_a", 400, verdict="WA", accepted=False, contest_key="abc1"),
        ])
        self.db.commit()
        atc, kn, pending, gym = histories.recent_submissions(self.db, u)
        self.assertEqual((atc["verdict"], atc["accepted"]), ("WA", False))
        self.assertEqual(kn["verdict"], "PT 60/100")
        self.assertTrue(pending["pending"])
        self.assertEqual((gym["mode"], gym["team"], gym["problem"]), ("virtual", "[UAIC] team", "105427A. Aperiodic"))

    def test_no_submissions(self):
        u = self.user("a")
        self.assertEqual(histories.recent_submissions(self.db, u), [])


class ProfilePage(unittest.TestCase):
    def setUp(self):
        reset_db()
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
        self.assertIn("(last 3, all platforms", html)


if __name__ == "__main__":
    unittest.main()
