"""Histories load when a handle is saved; the scheduler keeps them fresh."""
import asyncio
import re
import unittest
from datetime import datetime, timedelta
from unittest import mock

from fastapi.testclient import TestClient

from app import histories, models, scheduler, submissions
from app.database import SessionLocal
from app.main import app
from app.scraper import atcoder as ac_api
from app.scraper import kilonova as kn_api
from tests.support import DbTestCase, ac_raw, cf_raw, kn_raw, reset_db
from tests.test_sync import FakeCodeforces


class HandleChanges(DbTestCase):
    def test_new_user_loads_every_platform_they_have_a_handle_on(self):
        u = self.user("a", cf="cfh", kn="knh")
        self.assertEqual(histories.apply_handle_changes(self.db, u), ["codeforces", "kilonova"])

    def test_only_changed_handles_are_reloaded(self):
        u = self.user("a", cf="cfh", ac="ach")
        before = histories.handles_of(u)
        u.atcoder_handle = "ach2"
        u.kilonova_handle = "knh"
        self.assertEqual(histories.apply_handle_changes(self.db, u, before), ["atcoder", "kilonova"])

    def test_a_case_only_change_is_not_a_change(self):
        u = self.user("a", cf="Tourist")
        before = histories.handles_of(u)
        u.codeforces_handle = "tourist"
        self.assertEqual(histories.apply_handle_changes(self.db, u, before), [])

    def test_clearing_a_handle_drops_its_stored_data_and_leaves_the_rest(self):
        u = self.user("a", cf="cfh", ac="ach")
        for platform in ("codeforces", "atcoder"):
            self.db.add(models.Submission(user_id=u.id, platform=platform, submission_id=1, problem_key="1/A",
                                          submitted_at=1, verdict="AC", accepted=True))
            self.db.add(models.SubmissionSync(user_id=u.id, platform=platform, handle="x", submission_count=1))
        self.db.commit()
        before = histories.handles_of(u)
        u.atcoder_handle = None
        self.assertEqual(histories.apply_handle_changes(self.db, u, before), [])
        self.db.commit()
        left = {s.platform for s in self.db.query(models.Submission)}
        self.assertEqual(left, {"codeforces"})
        self.assertEqual({s.platform for s in self.db.query(models.SubmissionSync)}, {"codeforces"})

    def test_history_status_lists_only_platforms_with_a_handle(self):
        u = self.user("a", cf="cfh", kn="knh")
        self.db.add(models.SubmissionSync(user_id=u.id, platform="codeforces", handle="cfh", submission_count=7,
                                          last_synced_at=datetime(2026, 1, 2, 3, 4)))
        self.db.add(models.SubmissionSync(user_id=u.id, platform="kilonova", handle="knh", submission_count=0,
                                          last_error="Kilonova user 'knh' was not found"))
        self.db.commit()
        status = histories.history_status(self.db, u)
        self.assertEqual([(s["label"], s["count"], bool(s["synced_at"]), s["error"]) for s in status],
                         [("Codeforces", 7, True, None), ("Kilonova", 0, False, "Kilonova user 'knh' was not found")])


class HandleSaveFlows(unittest.TestCase):
    """Saving a handle through the site loads the history (background tasks run before the test client returns)."""

    def setUp(self):
        reset_db()
        for target in ("app.scheduler.start",):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        self.cf = FakeCodeforces(self)
        self.cf.status["newcf"] = [cf_raw(2, 1, "A", "OK"), cf_raw(1, 1, "A", "WRONG_ANSWER")]
        self.cf.status["oldcf"] = [cf_raw(9, 5, "A", "OK")]
        self.ac_calls, self.kn_calls = [], []

        async def ac_subs(handle, from_epoch=0):
            self.ac_calls.append(handle)
            return [ac_raw(1, "abc100_a", "AC")]

        async def ac_history(handle):
            return []

        async def kn_uid(name):
            return 5 if name == "andrei" else None

        async def kn_page(uid, offset=0):
            return [kn_raw(1, 100, 100)], 1

        for mod, name, fn in [(ac_api, "get_user_submissions", ac_subs), (ac_api, "get_rating_history", ac_history),
                              (kn_api, "get_user_id", kn_uid), (kn_api, "get_user_submissions", kn_page)]:
            p = mock.patch.object(mod, name, fn)
            p.start()
            self.addCleanup(p.stop)

        async def validate(handle):
            return {"rating": 1500, "rank": "specialist"} if handle in ("newcf", "oldcf") else None

        p = mock.patch("app.scraper.codeforces.validate_handle", validate)
        p.start()
        self.addCleanup(p.stop)

        ctx = TestClient(app, follow_redirects=False)
        self.admin = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.admin.post("/login", data={"username": "admin", "password": "test-admin-pw"})

    def counts(self, username):
        db = SessionLocal()
        try:
            u = db.query(models.User).filter_by(username=username).one()
            return {p: db.query(models.Submission).filter_by(user_id=u.id, platform=p).count()
                    for p in ("codeforces", "atcoder", "kilonova")}
        finally:
            db.close()

    def state(self, username, platform):
        db = SessionLocal()
        try:
            u = db.query(models.User).filter_by(username=username).one()
            s = submissions.sync_state(db, u.id, platform)
            return (s.submission_count, s.last_error) if s else None
        finally:
            db.close()

    def test_registration_loads_histories(self):
        c = TestClient(app, follow_redirects=False)
        r = c.post("/register", data={"username": "newbie", "password": "secret123", "password2": "secret123",
                                      "cf_handle": "newcf", "atcoder_handle": "ac_user", "kilonova_handle": "andrei"})
        self.assertEqual(r.status_code, 303, r.text[:200])
        self.assertEqual(self.counts("newbie"), {"codeforces": 2, "atcoder": 1, "kilonova": 1})

    def test_admin_creating_a_user_loads_histories(self):
        r = self.admin.post("/admin/users/new", data={"username": "made", "password": "secret123", "user_type": "user",
                                                       "cf_handle": "newcf"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.counts("made")["codeforces"], 2)

    def test_profile_edit_loads_only_what_changed_and_the_page_shows_it(self):
        self.admin.post("/admin/users/new", data={"username": "u", "password": "secret123", "user_type": "user",
                                                   "cf_handle": "oldcf", "atcoder_handle": "ac_user"})
        self.assertEqual(self.counts("u"), {"codeforces": 1, "atcoder": 1, "kilonova": 0})
        ac_before = len(self.ac_calls)
        r = self.admin.post("/users/u/edit", data={"full_name": "", "cf_handle": "oldcf", "atcoder_handle": "ac_user",
                                                    "kilonova_handle": "andrei"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.counts("u"), {"codeforces": 1, "atcoder": 1, "kilonova": 1})
        self.assertEqual(len(self.ac_calls), ac_before)  # atcoder handle unchanged: not reloaded
        page = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", self.admin.get("/users/u").text))
        self.assertIn("KN andrei", page)                            # the judge chip: handle ...
        self.assertIn("1 submission ", page)                         # ... how much is stored

    def test_a_wrong_handle_shows_its_error_on_the_profile(self):
        self.admin.post("/admin/users/new", data={"username": "u", "password": "secret123", "user_type": "user"})
        self.admin.post("/users/u/edit", data={"full_name": "", "cf_handle": "", "atcoder_handle": "",
                                                "kilonova_handle": "amcbn06"})
        self.assertEqual(self.state("u", "kilonova")[1], "Kilonova user 'amcbn06' was not found")
        page = re.sub(r"\s+", " ", self.admin.get("/users/u").text)
        self.assertIn("Kilonova user &#39;amcbn06&#39; was not found", page)
        # fixing the handle loads the history and clears the error
        self.admin.post("/users/u/edit", data={"full_name": "", "cf_handle": "", "atcoder_handle": "",
                                                "kilonova_handle": "andrei"})
        self.assertEqual(self.state("u", "kilonova"), (1, None))

    def test_clearing_a_handle_removes_its_history(self):
        self.admin.post("/admin/users/new", data={"username": "u", "password": "secret123", "user_type": "user",
                                                   "atcoder_handle": "ac_user"})
        self.assertEqual(self.counts("u")["atcoder"], 1)
        self.admin.post("/users/u/edit", data={"full_name": "", "cf_handle": "", "atcoder_handle": "", "kilonova_handle": ""})
        self.assertEqual(self.counts("u")["atcoder"], 0)
        self.assertIsNone(self.state("u", "atcoder"))

    def test_admin_edit_form_changing_the_codeforces_handle_reloads(self):
        self.admin.post("/admin/users/new", data={"username": "u", "password": "secret123", "user_type": "user", "cf_handle": "oldcf"})
        db = SessionLocal(); uid = db.query(models.User).filter_by(username="u").one().id; db.close()
        self.admin.post(f"/admin/users/{uid}/edit", data={"username": "u", "user_type": "user", "cf_handle": "newcf"})
        db = SessionLocal()
        ids = {s.submission_id for s in db.query(models.Submission).filter_by(user_id=uid)}
        db.close()
        self.assertEqual(ids, {1, 2})  # oldcf's submission (id 9) is gone, newcf's are there

    def test_reload_button_refetches_and_is_limited_to_who_may_edit(self):
        self.admin.post("/admin/users/new", data={"username": "u", "password": "secret123", "user_type": "user", "cf_handle": "oldcf"})
        self.admin.post("/admin/users/new", data={"username": "other", "password": "secret123", "user_type": "user"})
        self.cf.status["oldcf"].insert(0, cf_raw(10, 5, "B", "OK"))
        r = self.admin.post("/users/u/reload-history")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.counts("u")["codeforces"], 2)
        stranger = TestClient(app, follow_redirects=False)
        stranger.post("/login", data={"username": "other", "password": "secret123"})
        self.assertEqual(stranger.post("/users/u/reload-history").status_code, 403)


class Scheduler(DbTestCase):
    def test_the_auto_sync_job_is_not_paused(self):
        async def run():
            scheduler.start()
            try:
                jobs = {j.func.__name__: j for j in scheduler._scheduler.get_jobs()}
                for name, job in jobs.items():
                    self.assertIsNotNone(job.next_run_time, f"{name} would never run")
                self.assertIn("_auto_sync_all", jobs)
                self.assertEqual(jobs["_auto_sync_all"].trigger.interval, timedelta(hours=2))
            finally:
                scheduler.stop()
        with mock.patch("app.scheduler.rec.refresh_contest_metadata", mock.AsyncMock()):
            asyncio.run(run())

    def test_default_interval_is_two_hours_and_bad_values_fall_back(self):
        for value, expected in [(None, 2.0), ("", 2.0), ("4", 4.0), ("0.5", 0.5), ("junk", 2.0)]:
            with self.subTest(value=value):
                env = {} if value is None else {"SYNC_INTERVAL_HOURS": value}
                with mock.patch.dict("os.environ", env, clear=False):
                    if value is None:
                        import os
                        os.environ.pop("SYNC_INTERVAL_HOURS", None)
                    self.assertEqual(scheduler._interval_hours(), expected)

    async def test_run_refreshes_every_users_history_then_stale_items_only(self):
        cf = FakeCodeforces(self)
        cf.contests["1"] = ("R1", [{"index": "A", "name": "A"}])
        cf.status["a_cf"] = [cf_raw(1, 1, "A", "OK", name="A")]
        cf.status["b_cf"] = [cf_raw(2, 1, "A", "OK", name="A")]
        a, b = self.user("a", cf="a_cf"), self.user("b", cf="b_cf")
        self.user("nohandle")
        _, assignment = self.group([a])  # b is in no group: only the user-level refresh reaches them
        stale = self.item(assignment, "codeforces", "problem", "1/A")
        stale.sync_status, stale.last_synced_at = "done", datetime.utcnow() - timedelta(hours=1, minutes=58)
        fresh = self.item(assignment, "codeforces", "problem", "1/B")
        fresh.sync_status, fresh.last_synced_at = "done", datetime.utcnow() - timedelta(minutes=10)
        failed = self.item(assignment, "codeforces", "problem", "1/C")
        failed.sync_status, failed.last_synced_at = "error", None
        self.db.commit()

        await scheduler._auto_sync_all()

        self.assertEqual(cf.count("status:a_cf"), 1)  # once, though two items needed it
        self.assertEqual(cf.count("status:b_cf"), 1)  # b has no items but their history is kept current
        self.assertEqual(cf.count("status:nohandle"), 0)
        self.db.expire_all()
        self.assertEqual(self.db.get(models.AssignmentItem, stale.id).sync_status, "done")
        self.assertGreater(self.db.get(models.AssignmentItem, stale.id).last_synced_at, datetime.utcnow() - timedelta(minutes=1))
        self.assertLess(self.db.get(models.AssignmentItem, fresh.id).last_synced_at, datetime.utcnow() - timedelta(minutes=9))
        self.assertIsNotNone(self.db.get(models.AssignmentItem, failed.id).last_synced_at)  # errors are retried

    async def test_a_second_run_soon_after_costs_no_judge_calls(self):
        cf = FakeCodeforces(self)
        cf.status["a_cf"] = []
        self.user("a", cf="a_cf")
        await scheduler._auto_sync_all()
        await scheduler._auto_sync_all()
        self.assertEqual(cf.count("status:a_cf"), 1)


if __name__ == "__main__":
    unittest.main()
