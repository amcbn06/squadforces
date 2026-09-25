"""The profile page (compact layout, judge chips, closed dropdown, heatmap data) and the top navigation."""
import re
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

from fastapi.testclient import TestClient

from app import activity, models
from app.database import SessionLocal
from app.main import app
from tests.support import DbTestCase, reset_db


def flat(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


class ProfileSite(unittest.TestCase):
    def setUp(self):
        reset_db()
        for target in ("app.scheduler.start", "app.histories.load_histories"):
            p = mock.patch(target, mock.AsyncMock() if "histories" in target else mock.DEFAULT)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("app.auth.PBKDF2_ITERATIONS", 1000)
        p.start()
        self.addCleanup(p.stop)
        ctx = TestClient(app, follow_redirects=False)
        self.admin = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.admin.post("/login", data={"username": "admin", "password": "test-admin-pw"})
        self.admin.post("/admin/users/new", data={"username": "ana", "password": "secret-pass1", "user_type": "user",
                                                   "cf_handle": "", "atcoder_handle": "", "kilonova_handle": ""})
        self.admin.post("/admin/users/new", data={"username": "bo", "password": "secret-pass1", "user_type": "user"})
        self.ana = self.login("ana")
        self.bo = self.login("bo")
        db = SessionLocal()
        self.ana_id = db.query(models.User).filter_by(username="ana").one().id
        db.close()

    def login(self, name):
        c = TestClient(app, follow_redirects=False)
        assert c.post("/login", data={"username": name, "password": "secret-pass1"}).status_code == 303
        return c

    def give_handles_and_history(self, **counts):
        """Handles for ana on every judge, and stored submissions dated today, so nothing is fetched."""
        now = int(time.time())
        db = SessionLocal()
        user = db.get(models.User, self.ana_id)
        user.codeforces_handle, user.atcoder_handle, user.kilonova_handle = "ana_cf", "ana_ac", "ana_kn"
        user.cf_rank = "specialist"
        sid = 0
        for platform, n in counts.items():
            for i in range(n):
                sid += 1
                db.add(models.Submission(user_id=self.ana_id, platform=platform, submission_id=sid, problem_key=f"{i}/A",
                                         submitted_at=now - i * 60, verdict="AC", accepted=True, final=True))
            db.add(models.SubmissionSync(user_id=self.ana_id, platform=platform,
                                         handle={"codeforces": "ana_cf", "atcoder": "ana_ac", "kilonova": "ana_kn"}[platform],
                                         submission_count=n, last_synced_at=datetime.utcnow() - timedelta(minutes=3)))
        db.commit()
        db.close()

    # navigation --------------------------------------------------------------------------------

    def test_the_nav_has_no_profile_link_and_the_username_opens_the_profile(self):
        for client, name in ((self.ana, "ana"), (self.admin, "admin")):
            page = client.get("/groups/").text
            nav = page.split('<div id="nav">')[1].split('<div id="page">')[0]
            self.assertNotIn(">Profile<", nav)                                    # the link after Help is gone
            self.assertIn('<a href="/profile" title="Your profile"', nav)         # the username goes to the profile
            self.assertNotIn('href="/account/password"', nav)                     # no longer the change-password page
            self.assertIn(name, nav)
        r = self.ana.get("/profile")
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/users/ana"))

    def test_change_password_is_still_reachable_from_your_own_profile(self):
        self.assertIn('href="/account/password"', self.ana.get("/users/ana").text)
        self.assertNotIn('href="/account/password"', self.bo.get("/users/ana").text)       # not offered on someone else's

    # the page ----------------------------------------------------------------------------------

    def test_one_chip_per_judge_with_handle_size_and_freshness(self):
        self.give_handles_and_history(codeforces=12, atcoder=1, kilonova=1234)
        page = self.ana.get("/users/ana").text
        text = flat(page)
        for expected in ("CF ana_cf specialist", "AC ana_ac", "KN ana_kn"):
            self.assertIn(expected, text)
        self.assertIn("12 submissions", text)
        self.assertIn("1 submission ", text)                                      # singular
        self.assertIn("1,234 submissions", text)
        self.assertIn("3 minutes ago", text)
        for href in ("https://codeforces.com/profile/ana_cf", "https://atcoder.jp/users/ana_ac", "https://kilonova.ro/profile/ana_kn"):
            self.assertIn(f'href="{href}"', page)

    def test_a_failed_load_shows_next_to_its_judge(self):
        db = SessionLocal()
        db.get(models.User, self.ana_id).kilonova_handle = "nope"
        db.add(models.SubmissionSync(user_id=self.ana_id, platform="kilonova", handle="nope", submission_count=0,
                                     last_error="Kilonova user 'nope' was not found"))
        db.commit()
        db.close()
        text = flat(self.ana.get("/users/ana").text)
        self.assertIn("KN nope", text)
        self.assertIn("was not found", text)

    def test_reload_is_offered_to_the_owner_and_the_admin_only(self):
        self.give_handles_and_history(codeforces=1)
        for client, shown in ((self.ana, True), (self.admin, True), (self.bo, False)):
            self.assertEqual("/users/ana/reload-history" in client.get("/users/ana").text, shown)

    def test_recent_submissions_is_a_dropdown_that_starts_closed(self):
        self.give_handles_and_history(codeforces=3)
        page = self.ana.get("/users/ana").text
        m = re.search(r'<details class="box" id="recent-submissions"([^>]*)>', page)
        self.assertIsNotNone(m)
        self.assertNotIn("open", m.group(1))                                       # closed until clicked
        self.assertIn("Recent submissions", page)
        self.assertIn("(last 3, all platforms", page)

    def test_the_activity_box_carries_the_stats_and_all_three_legend_entries(self):
        page = self.ana.get("/users/ana").text
        box = page.split("<span>Activity</span>")[1].split('<div class="box-body"')[0]
        for text in ("current streak", "longest", "active days", "Codeforces", "AtCoder", "Kilonova"):
            self.assertIn(text, box)
        self.assertIn('id="heatmap-container"', page)
        self.assertNotIn(">Submission history<", page)                             # folded into the chips
        self.assertNotIn("Current streak</div>", page)                             # the three big tiles are gone

    def test_the_profile_of_someone_without_handles_still_renders(self):
        page = self.bo.get("/users/bo")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("class=\"chips\"", page.text)

    # the heatmap's data ------------------------------------------------------------------------

    def test_the_heatmap_data_includes_kilonova(self):
        self.give_handles_and_history(codeforces=2, atcoder=1, kilonova=4)
        data = self.ana.get("/users/ana/activity.json").json()
        totals = {k: sum(day[k] for day in data.values()) for k in ("cf", "atc", "kn")}
        self.assertEqual(totals, {"cf": 2, "atc": 1, "kn": 4})
        for day in data.values():
            self.assertEqual(set(day), {"cf", "atc", "kn"})                        # every day has all three counts

    def test_a_judge_without_a_handle_contributes_nothing(self):
        self.give_handles_and_history(codeforces=2)
        db = SessionLocal()
        db.get(models.User, self.ana_id).kilonova_handle = None
        db.commit()
        db.close()
        data = self.ana.get("/users/ana/activity.json").json()
        self.assertEqual(sum(day["kn"] for day in data.values()), 0)


class ActivityUnit(unittest.TestCase):
    def test_the_platform_keys_the_page_script_expects(self):
        self.assertEqual(activity.HEATMAP_PLATFORMS, {"codeforces": "cf", "atcoder": "atc", "kilonova": "kn"})


if __name__ == "__main__":
    unittest.main()
