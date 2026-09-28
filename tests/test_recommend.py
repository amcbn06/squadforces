"""The Recommend page: Codeforces and AtCoder as two separately-graded pools cached in their own DB tables, the
member dropdown's kind/platform filtering and defaults, and the "submissions" filter. Both judges are faked —
nothing here calls Codeforces or AtCoder.
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


def _atc_contest(db, cid, name, division, *, problems=None, fetched=True, start=1_700_000_000):
    """problems: {problem_id: (index, rating)}."""
    c = models.AtcContest(id=cid, name=name, start_time=start, division=division, problems_fetched=fetched)
    db.add(c)
    db.flush()
    for pid, (index, rating) in (problems or {}).items():
        db.add(models.AtcContestProblem(contest_id=cid, problem_id=pid, index=index, rating=rating))
    db.commit()
    return c


class FakeAtCoderCatalog:
    """Patches app.scraper.atcoder's two catalog reads, used by refresh_atc_contest_metadata() and
    prefetch_atc_contest_problems() to build the AtcContest/AtcContestProblem cache."""

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


class EffectiveRating(DbTestCase):
    def test_falls_back_to_the_unrated_floor(self):
        rated = self.user("alice", cf="alice_cf")
        rated.cf_rating = 1850
        unrated = self.user("bob", cf="bob_cf", ac="bob_ac")  # linked but never rated on either judge
        self.db.commit()
        self.assertEqual(rec.effective_rating(rated, "cf"), 1850)
        self.assertEqual(rec.effective_rating(unrated, "cf"), 800)
        self.assertEqual(rec.effective_rating(unrated, "atc"), 800)


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

    async def test_progress_is_reported_regardless_of_sub_filter(self):
        self.db.add(models.Submission(user_id=self.alice.id, platform="codeforces", submission_id=1,
                                       problem_key="100/A", contest_key="100", submitted_at=1, accepted=True,
                                       verdict="AC"))
        self.db.commit()
        recs = {r["contest"].id: r["progress"] for r in
                rec.get_cf_recommendations(self.db, ["div2"], 1000, account=self.alice)}
        self.assertEqual(recs[100], {"solved": 1, "total": 3})
        self.assertEqual(recs[101], {"solved": 0, "total": 2})

    async def test_progress_is_none_without_exact_data_or_an_account(self):
        recs = {r["contest"].id: r["progress"] for r in
                rec.get_cf_recommendations(self.db, ["div2", "div3"], 1000, account=self.alice)}
        self.assertIsNone(recs[200])  # no cached problem set
        recs_no_account = {r["contest"].id: r["progress"] for r in
                            rec.get_cf_recommendations(self.db, ["div2"], 1000)}
        self.assertIsNone(recs_no_account[100])  # nobody to check


class AtcPool(DbTestCase):
    """get_atc_recommendations() reads the AtcContest/AtcContestProblem cache — same shape and same tests as
    CfPool, just against the other table."""

    def setUp(self):
        super().setUp()
        _atc_contest(self.db, "abc343", "ABC 343", "abc",
                     problems={"abc343_a": ("A", 100), "abc343_b": ("B", 400), "abc343_c": ("C", 900)})
        _atc_contest(self.db, "arc180", "ARC 180", "arc",
                     problems={"arc180_a": ("A", 1900), "arc180_b": ("B", 2200)}, start=1_700_100_000)
        _atc_contest(self.db, "agc010", "AGC 010", "agc", problems={"agc010_a": ("A", 1200)}, fetched=False)
        self.alice = self.user("alice", ac="alice_ac")

    def test_grades_and_sorts_best_score_first(self):
        recs = rec.get_atc_recommendations(self.db, ["abc", "arc"], user_rating=100)
        self.assertEqual([r["contest"].id for r in recs], ["abc343", "arc180"])
        self.assertTrue(recs[0]["grade"]["exact"])

    def test_an_uncached_contest_falls_back_to_the_typical_distribution(self):
        recs = rec.get_atc_recommendations(self.db, ["agc"], user_rating=1200)
        self.assertEqual(len(recs), 1)
        self.assertFalse(recs[0]["grade"]["exact"])

    def test_division_filter_reads_the_contest_id_prefix(self):
        recs = rec.get_atc_recommendations(self.db, ["abc"], user_rating=100)
        self.assertEqual([r["contest"].id for r in recs], ["abc343"])
        self.assertEqual(recs[0]["contest"].division, "abc")

    def test_grading_reuses_the_same_ideal_stretch_bands_as_codeforces(self):
        recs = rec.get_atc_recommendations(self.db, ["abc"], user_rating=100)
        g = recs[0]["grade"]
        self.assertTrue(g["exact"])
        # abc343_a (diff 0) and abc343_b (diff 300) both land in the 0..300 ideal band; abc343_c (diff 800) doesn't.
        self.assertEqual(g["in_zone"], 2)
        self.assertEqual(g["stretch"], 0)

    def test_submission_filter_buckets(self):
        self.db.add(models.Submission(user_id=self.alice.id, platform="atcoder", submission_id=1,
                                       problem_key="abc343_a", contest_key="abc343", submitted_at=1,
                                       accepted=True, verdict="AC"))
        self.db.commit()
        solved = rec.get_atc_recommendations(self.db, ["abc", "arc"], 900, account=self.alice, sub_filter="solved")
        self.assertEqual([r["contest"].id for r in solved], ["abc343"])
        untouched = rec.get_atc_recommendations(self.db, ["abc", "arc"], 900, account=self.alice, sub_filter="none")
        self.assertEqual([r["contest"].id for r in untouched], ["arc180"])

    def test_progress_is_reported_regardless_of_sub_filter(self):
        self.db.add(models.Submission(user_id=self.alice.id, platform="atcoder", submission_id=1,
                                       problem_key="abc343_a", contest_key="abc343", submitted_at=1,
                                       accepted=True, verdict="AC"))
        self.db.commit()
        recs = {r["contest"].id: r["progress"] for r in
                rec.get_atc_recommendations(self.db, ["abc", "arc"], 900, account=self.alice)}
        self.assertEqual(recs["abc343"], {"solved": 1, "total": 3})
        self.assertEqual(recs["arc180"], {"solved": 0, "total": 2})


class AtcCacheBuilding(DbTestCase):
    """refresh_atc_contest_metadata() / prefetch_atc_contest_problems(): unlike Codeforces's rate-limited
    equivalents, both read an already-cached in-process catalog, so a whole backlog is fetched in one pass."""

    def setUp(self):
        super().setUp()
        self.atc = FakeAtCoderCatalog(self)
        self.atc.contests = [
            {"id": "abc343", "title": "ABC 343", "start_epoch_second": 1_700_000_000, "duration_second": 6000,
             "rate_change": "~ 1999"},
            {"id": "old001", "title": "Unrated Practice", "start_epoch_second": 1_600_000_000,
             "duration_second": 3600, "rate_change": "-"},  # unrated: excluded
        ]
        self.atc.tasks["abc343"] = [{"id": "abc343_a", "contest_index": "A", "difficulty": 800}]

    async def test_metadata_refresh_keeps_only_rated_contests(self):
        added = await rec.refresh_atc_contest_metadata(self.db)
        self.assertEqual(added, 1)
        self.assertEqual([c.id for c in self.db.query(models.AtcContest).all()], ["abc343"])

    async def test_bootstrap_fetches_every_contest_in_one_pass(self):
        await rec.bootstrap_atc_cache()
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            contest = db.get(models.AtcContest, "abc343")
            self.assertTrue(contest.problems_fetched)
            self.assertEqual([(p.problem_id, p.rating) for p in contest.problems], [("abc343_a", 800)])
        finally:
            db.close()


class RefreshProfile(DbTestCase):
    async def test_atcoder_caches_the_latest_rated_rating(self):
        from app.platforms.atc import PLATFORM
        user = self.user("alice", ac="alice_ac")
        history = [
            {"IsRated": True, "NewRating": 1200},
            {"IsRated": False, "NewRating": None},  # an unrated contest in between changes nothing
            {"IsRated": True, "NewRating": 1450},
        ]
        with mock.patch.object(atc_api, "get_rating_history", mock.AsyncMock(return_value=history)):
            await PLATFORM.refresh_profile(user, "alice_ac")
        self.assertEqual(user.atc_rating, 1450)

    async def test_stays_none_when_never_rated(self):
        from app.platforms.atc import PLATFORM
        user = self.user("alice", ac="alice_ac")
        with mock.patch.object(atc_api, "get_rating_history", mock.AsyncMock(return_value=[])):
            await PLATFORM.refresh_profile(user, "alice_ac")
        self.assertIsNone(user.atc_rating)


class RecommendPageHTTP(unittest.TestCase):
    """The two tabs, the member dropdown and the submission filter, end to end through the real routes."""

    def setUp(self):
        reset_db()
        for target, repl in [
            ("app.scheduler.start", mock.DEFAULT),
            ("app.recommend.bootstrap_cache", mock.AsyncMock()),
            ("app.recommend.bootstrap_atc_cache", mock.AsyncMock()),
            ("app.histories.load_histories", mock.AsyncMock()),  # admin/users/new would otherwise hit Codeforces
            ("app.auth.PBKDF2_ITERATIONS", 1000),  # several accounts per test; keep hashing cheap
        ]:
            p = mock.patch(target, repl) if repl is not mock.DEFAULT else mock.patch(target)
            p.start()
            self.addCleanup(p.stop)

        self.client_ctx = TestClient(app, follow_redirects=False)
        self.admin = self.client_ctx.__enter__()
        self.addCleanup(self.client_ctx.__exit__, None, None, None)
        r = self.admin.post("/login", data={"username": "admin", "password": "test-admin-pw"})
        self.assertEqual(r.status_code, 303)

    def make_user(self, name, *, user_type="user", cf="", ac=""):
        r = self.admin.post("/admin/users/new", data={
            "username": name, "password": "secret123", "user_type": user_type,
            "cf_handle": cf, "atcoder_handle": ac,
        })
        self.assertEqual(r.status_code, 303, r.text)

    def login(self, name):
        c = TestClient(app, follow_redirects=False)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        r = c.post("/login", data={"username": name, "password": "secret123"})
        self.assertEqual(r.status_code, 303)
        return c

    def options_of(self, html: str) -> list[str]:
        """Usernames listed in the Member <select>."""
        select = re.search(r'id="rc-member".*?</select>', html, re.S).group(0)
        return re.findall(r'<option value="\d+"[^>]*>\s*([^\s(]+)', select)

    def is_selected(self, html: str, user_id: int) -> bool:
        tag = re.search(r'<option value="{}"[^>]*>'.format(user_id), html)
        return bool(tag) and "selected" in tag.group(0)

    def test_the_codeforces_tab_is_the_default(self):
        r = self.admin.get("/recommend")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Codeforces", r.text)
        self.assertIn("platform=atc", r.text)

    def test_the_submission_filter_buttons_are_present(self):
        r = self.admin.get("/recommend")
        self.assertIn("Not attempted", r.text)
        self.assertIn("Solved something", r.text)

    def test_member_dropdown_only_shows_the_viewers_own_kind_and_platform(self):
        self.make_user("alice_u", user_type="user", cf="alice_cf")       # same kind, cf-linked: visible on cf tab
        self.make_user("bob_u", user_type="user", ac="bob_ac")           # same kind, atc-linked: visible on atc tab
        self.make_user("carol_student", user_type="student", cf="carol_cf")  # different kind: never visible to a user
        self.make_user("dave_u", user_type="user")                       # same kind, no handle at all: never visible

        viewer = self.login("alice_u")
        cf_page = viewer.get("/recommend", params={"platform": "cf"}).text
        self.assertEqual(set(self.options_of(cf_page)), {"alice_u"})

        atc_page = viewer.get("/recommend", params={"platform": "atc"}).text
        self.assertEqual(set(self.options_of(atc_page)), {"bob_u"})

    def test_admin_sees_both_kinds(self):
        self.make_user("alice_u", user_type="user", cf="alice_cf")
        self.make_user("carol_student", user_type="student", cf="carol_cf")
        page = self.admin.get("/recommend", params={"platform": "cf"}).text
        self.assertEqual(set(self.options_of(page)), {"alice_u", "carol_student"})

    def test_the_current_user_is_auto_selected_when_eligible(self):
        self.make_user("alice_u", user_type="user", cf="alice_cf")
        viewer = self.login("alice_u")
        page = viewer.get("/recommend", params={"platform": "cf"}).text
        self.assertTrue(self.is_selected(page, self._user_id("alice_u")))

    def test_the_selection_is_blank_when_the_viewer_is_not_eligible(self):
        self.make_user("alice_u", user_type="user")  # no Codeforces handle -> not eligible on the cf tab
        viewer = self.login("alice_u")
        page = viewer.get("/recommend", params={"platform": "cf"}).text
        # nobody eligible at all, so the dropdown has no options and the blank one is implicitly selected
        self.assertEqual(self.options_of(page), [])

    def test_unrated_but_linked_members_show_the_800_floor(self):
        self.make_user("alice_u", user_type="user", cf="alice_cf")  # cf_rating stays None: never rated
        page = self.admin.get("/recommend", params={"platform": "cf"}).text
        self.assertIn("alice_u (800)", page)

    def test_a_typed_rating_wins_over_the_selected_members_rating(self):
        from app.database import SessionLocal
        db = SessionLocal()
        _cf_contest(db, 100, "Div 2 A", "div2", problems={"A": 800})
        db.close()

        self.make_user("alice_u", user_type="user", cf="alice_cf")
        mid = self._user_id("alice_u")
        r = self.admin.get("/recommend", params={"platform": "cf", "member_id": mid, "rating": "1750"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("picks for rating 1750", r.text)

    def test_the_submission_filter_checks_the_selected_member_not_the_viewer(self):
        # Reproduces the reported bug: the admin (no handle) selects a member who does have one and filters —
        # this must check the *member's* submissions, not fail with "no handle on your account".
        self.make_user("alice_u", user_type="user", cf="alice_cf")
        mid = self._user_id("alice_u")
        r = self.admin.get("/recommend", params={
            "platform": "cf", "member_id": mid, "sub_filter": "solved", "cf_divisions": "div2",
        })
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("No Codeforces handle is set on your account", r.text)
        self.assertNotIn("alice_u has no Codeforces handle set", r.text)  # alice_u DOES have one

    def test_a_solved_contest_shows_a_checkmark(self):
        from app.database import SessionLocal
        db = SessionLocal()
        _cf_contest(db, 100, "Div 2 A", "div2", problems={"A": 800})
        db.close()
        self.make_user("alice_u", user_type="user", cf="alice_cf")
        mid = self._user_id("alice_u")
        db = SessionLocal()
        db.add(models.Submission(user_id=mid, platform="codeforces", submission_id=1, problem_key="100/A",
                                  contest_key="100", submitted_at=1, accepted=True, verdict="AC"))
        db.commit()
        db.close()

        r = self.admin.get("/recommend", params={"platform": "cf", "member_id": mid, "cf_divisions": "div2"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("✓ solved", r.text)

    def _user_id(self, username):
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            return db.query(models.User).filter_by(username=username).first().id
        finally:
            db.close()
