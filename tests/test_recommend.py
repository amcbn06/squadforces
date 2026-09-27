"""The Recommend page: Codeforces and AtCoder as two separately-graded pools, and the "your submissions" filter.
Both judges are faked — nothing here calls Codeforces or AtCoder.
"""
import re
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app import models, recommend as rec
from app.main import app
from app.scraper import atcoder as atc_api
from tests.support import DbTestCase, reset_db


def _cf_contest(db, cid, name, division, *, problems=None, fetched=True, start=1_700_000_000):
    c = models.CfContest(id=cid, name=name, start_time=start, division=division, problems_fetched=fetched)
    db.add(c)
    db.flush()
    for index, rating in (problems or {}).items():
        db.add(models.CfContestProblem(contest_id=cid, index=index, rating=rating))
    db.commit()
    return c


class FakeAtCoderCatalog:
    """Patches app.scraper.atcoder's two catalog reads. Both are served from an in-process cache on the real
    module, so recommend.py never needs more than these to grade the whole AtCoder pool."""

    def __init__(self, testcase):
        self.contests: list[dict] = []
        self.tasks: dict[str, list[dict]] = {}
        self.calls: list[str] = []

        async def get_all_contests():
            self.calls.append("contests")
            return self.contests

        async def get_contest_tasks(contest_id):
            self.calls.append(f"tasks:{contest_id}")
            return self.tasks.get(contest_id, [])

        for name, fn in [("get_all_contests", get_all_contests), ("get_contest_tasks", get_contest_tasks)]:
            p = mock.patch.object(atc_api, name, fn)
            p.start()
            testcase.addCleanup(p.stop)


class DivisionDetection(unittest.TestCase):
    def test_atcoder_contests_are_grouped_by_id_prefix(self):
        self.assertEqual(rec.detect_atc_division("abc343"), "abc")
        self.assertEqual(rec.detect_atc_division("arc180"), "arc")
        self.assertEqual(rec.detect_atc_division("agc065"), "agc")
        self.assertEqual(rec.detect_atc_division("dp"), "other")


class SubmissionBucket(unittest.TestCase):
    def test_the_three_buckets(self):
        sub = lambda accepted: mock.Mock(accepted=accepted)
        self.assertEqual(rec._submission_bucket([]), "none")
        self.assertEqual(rec._submission_bucket([sub(False)]), "attempted")
        self.assertEqual(rec._submission_bucket([sub(False), sub(True)]), "solved")


class CfPool(DbTestCase):
    def setUp(self):
        super().setUp()
        _cf_contest(self.db, 100, "Div 2 A", "div2", problems={"A": 800, "B": 1200, "C": 1600})
        _cf_contest(self.db, 101, "Div 2 B", "div2", problems={"A": 1900, "B": 2200}, start=1_700_100_000)
        _cf_contest(self.db, 200, "Div 3", "div3", problems={"A": 800}, fetched=False)  # never cached
        self.alice = self.user("alice", cf="alice_cf")

    async def test_grades_and_sorts_best_score_first(self):
        recs = rec.get_cf_recommendations(self.db, ["div2"], user_rating=1000)
        self.assertEqual([r["contest"].id for r in recs], [100, 101])
        self.assertTrue(recs[0]["grade"]["exact"])

    async def test_an_uncached_contest_falls_back_to_the_typical_distribution(self):
        recs = rec.get_cf_recommendations(self.db, ["div3"], user_rating=1000)
        self.assertEqual(len(recs), 1)
        self.assertFalse(recs[0]["grade"]["exact"])

    async def test_division_filter(self):
        recs = rec.get_cf_recommendations(self.db, ["div3"], user_rating=1000)
        self.assertEqual([r["contest"].id for r in recs], [200])

    async def test_submission_filter_needs_a_handle(self):
        nobody = self.user("bob")  # no codeforces handle
        recs = rec.get_cf_recommendations(self.db, ["div2"], 1000, account=nobody, sub_filter="none")
        self.assertEqual(recs, [])

    async def test_submission_filter_excludes_contests_without_cached_problems(self):
        recs = rec.get_cf_recommendations(self.db, ["div3"], 1000, account=self.alice, sub_filter="none")
        self.assertEqual(recs, [])  # contest 200 has no exact problem set to check submissions against

    async def test_submission_filter_buckets(self):
        self.db.add(models.Submission(user_id=self.alice.id, platform="codeforces", submission_id=1,
                                       problem_key="100/A", contest_key="100", submitted_at=1, accepted=True,
                                       verdict="AC"))
        self.db.commit()
        solved = rec.get_cf_recommendations(self.db, ["div2"], 1000, account=self.alice, sub_filter="solved")
        self.assertEqual([r["contest"].id for r in solved], [100])
        untouched = rec.get_cf_recommendations(self.db, ["div2"], 1000, account=self.alice, sub_filter="none")
        self.assertEqual([r["contest"].id for r in untouched], [101])


class AtcPool(DbTestCase):
    def setUp(self):
        super().setUp()
        self.atc = FakeAtCoderCatalog(self)
        self.atc.contests = [
            {"id": "abc343", "title": "ABC 343", "start_epoch_second": 1_700_000_000, "duration_second": 6000,
             "rate_change": "~ 1999"},
            {"id": "arc180", "title": "ARC 180", "start_epoch_second": 1_700_100_000, "duration_second": 7200,
             "rate_change": "All"},
            {"id": "old001", "title": "Unrated Practice", "start_epoch_second": 1_600_000_000,
             "duration_second": 3600, "rate_change": "-"},
        ]
        self.atc.tasks["abc343"] = [
            {"id": "abc343_a", "difficulty": 100}, {"id": "abc343_b", "difficulty": 400},
            {"id": "abc343_c", "difficulty": 900},
        ]
        self.atc.tasks["arc180"] = [{"id": "arc180_a", "difficulty": 1900}, {"id": "arc180_b", "difficulty": 2400}]
        self.alice = self.user("alice", ac="alice_ac")

    async def test_unrated_contests_are_excluded(self):
        recs = await rec.get_atc_recommendations(["abc", "arc"], user_rating=900)
        self.assertEqual({r["contest"]["id"] for r in recs}, {"abc343", "arc180"})

    async def test_division_filter_reads_the_contest_id_prefix(self):
        recs = await rec.get_atc_recommendations(["abc"], user_rating=900)
        self.assertEqual([r["contest"]["id"] for r in recs], ["abc343"])
        self.assertEqual(recs[0]["contest"]["division"], "abc")

    async def test_grading_reuses_the_same_ideal_stretch_bands_as_codeforces(self):
        recs = await rec.get_atc_recommendations(["abc"], user_rating=100)
        g = recs[0]["grade"]
        self.assertTrue(g["exact"])
        # abc343_a (diff 0) and abc343_b (diff 300) both land in the 0..300 ideal band; abc343_c (diff 800) doesn't.
        self.assertEqual(g["in_zone"], 2)
        self.assertEqual(g["stretch"], 0)

    async def test_submission_filter(self):
        self.db.add(models.Submission(user_id=self.alice.id, platform="atcoder", submission_id=1,
                                       problem_key="abc343_a", contest_key="abc343", submitted_at=1,
                                       accepted=True, verdict="AC"))
        self.db.commit()
        solved = await rec.get_atc_recommendations(["abc", "arc"], 900, db=self.db, account=self.alice,
                                                     sub_filter="solved")
        self.assertEqual([r["contest"]["id"] for r in solved], ["abc343"])
        untouched = await rec.get_atc_recommendations(["abc", "arc"], 900, db=self.db, account=self.alice,
                                                        sub_filter="none")
        self.assertEqual([r["contest"]["id"] for r in untouched], ["arc180"])


class RecommendPageHTTP(unittest.TestCase):
    """The two tabs render end to end, through the real routes and templates."""

    def setUp(self):
        reset_db()
        p = mock.patch("app.scheduler.start")  # startup would otherwise touch Codeforces
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch("app.recommend.bootstrap_cache", mock.AsyncMock())  # an empty CfContest table triggers it
        p.start()
        self.addCleanup(p.stop)
        self.atc = FakeAtCoderCatalog(self)
        self.atc.contests = [{"id": "abc343", "title": "ABC 343", "start_epoch_second": 1_700_000_000,
                              "duration_second": 6000, "rate_change": "~ 1999"}]
        self.atc.tasks["abc343"] = [{"id": "abc343_a", "difficulty": 800}]

        self.client_ctx = TestClient(app, follow_redirects=False)
        self.client = self.client_ctx.__enter__()
        self.addCleanup(self.client_ctx.__exit__, None, None, None)
        r = self.client.post("/login", data={"username": "admin", "password": "test-admin-pw"})
        self.assertEqual(r.status_code, 303)

    def test_the_codeforces_tab_is_the_default(self):
        r = self.client.get("/recommend")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Codeforces", r.text)
        self.assertIn("platform=atc", r.text)  # the AtCoder tab link is present

    def test_the_atcoder_tab_shows_its_own_pool(self):
        r = self.client.get("/recommend", params={"platform": "atc", "atc_divisions": "abc", "rating": "800"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("ABC 343", r.text)
        self.assertIn("atcoder.jp/contests/abc343", r.text)

    def test_the_submission_filter_buttons_are_present(self):
        r = self.client.get("/recommend")
        self.assertIn("Not attempted", r.text)
        self.assertIn("Solved something", r.text)
