"""The web layer: adding items, the matrix page, manual marks, the heatmap, user deletion."""
import re
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app import models
from app.database import SessionLocal
from app.main import app
from tests.support import cf_raw, reset_db
from tests.test_sync import FakeCodeforces


def flat(html: str) -> str:
    return re.sub(r"\s+", " ", html)


class WebTestCase(unittest.TestCase):
    def setUp(self):
        reset_db()
        # no item ever reaches a real judge from these tests unless a test installs a fake
        p = mock.patch("app.routers.assignments._run_sync", mock.AsyncMock())
        self.run_sync = p.start()
        self.addCleanup(p.stop)
        p = mock.patch("app.scheduler.start")  # its startup job would call Codeforces
        p.start()
        self.addCleanup(p.stop)

        self.client_ctx = TestClient(app, follow_redirects=False)
        self.admin = self.client_ctx.__enter__()
        self.addCleanup(self.client_ctx.__exit__, None, None, None)
        r = self.admin.post("/login", data={"username": "admin", "password": "test-admin-pw"})
        self.assertEqual(r.status_code, 303)
        r = self.admin.post("/groups/new", data={"name": "Team", "hints_allowed": "1"})
        self.gid = int(re.search(r"/groups/(\d+)", r.headers["location"]).group(1))
        for name in ("alice", "bob"):
            self.admin.post("/admin/users/new", data={
                "username": name, "password": "secret123", "user_type": "user", "group_ids": [self.gid],
                "atcoder_handle": "", "cf_handle": "",
            })
        r = self.admin.post("/assignments/new", data={"group_id": self.gid, "title": "Week 1"})
        self.aid = int(re.search(r"/assignments/(\d+)", r.headers["location"]).group(1))
        self.alice = self.login("alice")

    def login(self, name, password="secret123"):
        c = TestClient(app, follow_redirects=False)
        r = c.post("/login", data={"username": name, "password": password})
        self.assertEqual(r.status_code, 303, name)
        return c

    def add(self, client, platform, item_type, external_id, title=""):
        return client.post(f"/assignments/{self.aid}/items/add", data={
            "item_type": item_type, "platform": platform, "external_id": external_id, "title": title})

    def items(self):
        db = SessionLocal()
        try:
            return [(i.platform, i.type, i.external_id, i.title, i.source_url)
                    for i in db.query(models.AssignmentItem).order_by(models.AssignmentItem.id)]
        finally:
            db.close()

    def page(self):
        r = self.alice.get(f"/assignments/{self.aid}")
        self.assertEqual(r.status_code, 200)
        return r.text


class AddingItems(WebTestCase):
    def test_every_platform_through_the_form(self):
        edu = "https://codeforces.com/edu/course/2/lesson/4/3/practice/contest/274545/problem/A"
        cases = [
            ("codeforces", "contest", "https://codeforces.com/contest/2085", ("codeforces", "contest", "2085", None, None)),
            ("codeforces", "contest", "https://codeforces.com/gym/105427", ("codeforces", "contest", "105427", None, None)),
            ("codeforces", "problem", "https://codeforces.com/gym/105427/problem/A", ("codeforces", "problem", "105427/A", None, None)),
            ("codeforces", "problem", "1234b", ("codeforces", "problem", "1234/B", None, None)),
            ("codeforces", "problem", edu, ("codeforces", "problem", "274545/A", None, edu)),
            ("atcoder", "contest", "abc400", ("atcoder", "contest", "abc400", None, None)),
            ("atcoder", "problem", "https://atcoder.jp/contests/abc123/tasks/abc123_a", ("atcoder", "problem", "abc123_a", None, None)),
            ("kilonova", "problem", "https://kilonova.ro/problems/2460", ("kilonova", "problem", "2460", None, None)),
            ("kilonova", "contest", "1572", ("kilonova", "contest", "1572", None, None)),
            ("cses", "problem", "https://cses.fi/problemset/task/1068", ("cses", "problem", "1068", None, None)),
        ]
        for platform, kind, raw, expected in cases:
            with self.subTest(raw=raw):
                r = self.add(self.alice, platform, kind, raw)
                self.assertEqual(r.status_code, 303, r.text[:300])
                self.assertEqual(self.items()[-1], expected)
        self.assertEqual(self.run_sync.await_count, len(cases))  # every add starts a sync

    def test_other_site_link_with_a_title(self):
        r = self.add(self.alice, "other", "problem", "https://leetcode.com/problems/two-sum/", "Two Sum")
        self.assertEqual(r.status_code, 303)
        (item,) = self.items()
        self.assertEqual((item[0], item[1], item[3], item[4]), ("other", "problem", "Two Sum", "https://leetcode.com/problems/two-sum/"))

    def test_rejections_explain_themselves(self):
        cases = [
            ("codeforces", "contest", "nonsense", "Invalid Codeforces contest"),
            ("codeforces", "problem", "https://codeforces.com/contest/1234", "Invalid Codeforces problem"),
            ("atcoder", "problem", "###", "Invalid AtCoder problem"),
            ("kilonova", "problem", "x", "Invalid Kilonova problem"),
            ("cses", "contest", "1068", "no contests"),
            ("cses", "problem", "https://example.com/x", "Invalid CSES"),
            ("other", "problem", "javascript:alert(1)", "http(s) link"),
            ("other", "contest", "https://example.com/x", "Only problems"),
            ("spoj", "problem", "x", "Unknown platform"),
        ]
        for platform, kind, raw, message in cases:
            with self.subTest(raw=raw):
                r = self.add(self.alice, platform, kind, raw)
                self.assertEqual(r.status_code, 422)
                self.assertIn(message, r.text)
        self.assertEqual(self.items(), [])

    def test_bulk_add_a_mixed_paste(self):
        paste = "\n".join([
            "https://codeforces.com/gym/105427/problem/A",
            "https://codeforces.com/gym/105427",
            "https://codeforces.com/edu/course/2/lesson/4/3/practice/contest/274545/problem/A",
            "https://cses.fi/problemset/task/1144/",
            "https://atcoder.jp/contests/abc400/tasks/abc400_c",
            "https://kilonova.ro/problem_lists/1572",
            "https://example.org/some/problem",
        ])
        r = self.alice.post(f"/assignments/{self.aid}/items/bulk", data={"links": paste})
        self.assertEqual(r.status_code, 303)
        got = [(p, t, e) for p, t, e, _, _ in self.items()]
        self.assertEqual(got[:6], [
            ("codeforces", "problem", "105427/A"), ("codeforces", "contest", "105427"), ("codeforces", "problem", "274545/A"),
            ("cses", "problem", "1144"), ("atcoder", "problem", "abc400_c"), ("kilonova", "contest", "1572"),
        ])
        self.assertEqual(got[6][:2], ("other", "problem"))
        self.assertEqual(self.run_sync.await_count, 7)

    def test_bulk_add_reports_duplicates_and_failures(self):
        self.alice.post(f"/assignments/{self.aid}/items/bulk", data={"links": "https://cses.fi/problemset/task/1068"})
        r = self.alice.post(f"/assignments/{self.aid}/items/bulk", data={
            "links": "https://cses.fi/problemset/task/1068 https://cses.fi/problemset/list/ 1234A 999"})
        self.assertEqual(r.status_code, 200)
        text = flat(r.text)
        self.assertIn("Already in this assignment (skipped): CSES problem 1068", text)
        self.assertIn("single task", text)
        self.assertIn("ambiguous number", text)
        self.assertEqual(len(self.items()), 2)  # the CSES task and 1234A

    def test_bulk_add_limit(self):
        many = " ".join(f"https://cses.fi/problemset/task/{i}" for i in range(1, 60))
        r = self.alice.post(f"/assignments/{self.aid}/items/bulk", data={"links": many})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.items(), [])


class MatrixPage(WebTestCase):
    def setUp(self):
        super().setUp()
        for platform, kind, raw in [
            ("codeforces", "contest", "105427"), ("codeforces", "problem", "105427/A"), ("codeforces", "contest", "2085"),
            ("codeforces", "problem", "1234/A"), ("atcoder", "problem", "abc123_a"), ("atcoder", "contest", "abc400"),
            ("kilonova", "problem", "2460"), ("cses", "problem", "1068"),
        ]:
            self.add(self.alice, platform, kind, raw)
        self.add(self.alice, "other", "problem", "https://leetcode.com/problems/two-sum/")
        self.add(self.alice, "codeforces", "problem",
                 "https://codeforces.com/edu/course/2/lesson/4/3/practice/contest/274545/problem/A")

    def test_links_and_icons_come_from_the_platform_modules(self):
        html = self.page()
        for href in [
            "https://codeforces.com/gym/105427",                        # gym contest
            "https://codeforces.com/gym/105427/problem/A",              # gym problem
            "https://codeforces.com/contest/2085",
            "https://codeforces.com/problemset/problem/1234/A",
            "https://atcoder.jp/contests/abc123/tasks/abc123_a",
            "https://atcoder.jp/contests/abc400",
            "https://kilonova.ro/problems/2460",
            "https://cses.fi/problemset/task/1068",
            "https://leetcode.com/problems/two-sum/",
            "https://codeforces.com/edu/course/2/lesson/4/3/practice/contest/274545/problem/A",
        ]:
            self.assertIn(f'href="{href}"', html, href)
        for icon in ("/static/cf.png", "/static/ac.png", "/static/kn.png", "/static/cses-icon.png"):
            self.assertIn(icon, html)
        self.assertIn('class="picon picon-unknown"', html)  # the link to another site gets the "?" placeholder
        self.assertNotIn("/static/None", html)

    def test_the_dark_theme_lightens_the_atcoder_logo(self):
        css = open("static/style.css", encoding="utf-8").read()
        self.assertRegex(css, r'\[data-theme="dark"\] img\[src\$="/ac\.png"\]\s*\{[^}]*invert')

    def test_other_link_shows_its_url_as_the_title(self):
        self.assertIn(">https://leetcode.com/problems/two-sum/</a>", self.page())

    def test_platform_dropdown_lists_every_platform(self):
        html = self.page()
        for label in ("Codeforces", "AtCoder", "Kilonova", "CSES", "Other (link only)"):
            self.assertIn(label, html)

    def test_a_hostile_link_cannot_inject_markup(self):
        self.add(self.alice, "other", "problem", 'https://example.com/"onmouseover="alert(1)')
        self.add(self.alice, "other", "problem", "https://example.com/<script>alert(1)</script>")
        html = self.page()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn('"onmouseover="alert(1)', html)
        self.assertNotIn("javascript:", html.lower().replace("javascript:void", ""))

    def test_manual_marking_works_for_other_links_and_titles_can_be_set(self):
        db = SessionLocal()
        other = db.query(models.AssignmentItem).filter_by(platform="other").first()
        alice_id = db.query(models.User).filter_by(username="alice").one().id
        oid = other.id
        db.close()
        r = self.alice.post(f"/assignments/{self.aid}/items/{oid}/solved", data={"user_id": alice_id, "solved": "1"})
        self.assertEqual(r.status_code, 303)
        db = SessionLocal()
        self.assertTrue(db.query(models.Result).filter_by(assignment_item_id=oid, user_id=alice_id).one().solved)
        db.close()
        r = self.alice.post(f"/assignments/{self.aid}/items/{oid}/title", data={"title": "Two Sum (LC)"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("Two Sum (LC)", self.page())


class SubmissionStoreThroughTheApp(WebTestCase):
    """Add an item with a fake Codeforces behind it and watch the whole chain fill the store."""

    def setUp(self):
        super().setUp()
        self.run_sync.side_effect = self.real_sync  # let syncs run for real, against the fake judge
        self.cf = FakeCodeforces(self)
        self.cf.contests["1"] = ("Beta Round 1", [{"index": "A", "name": "Theatre Square", "rating": 1000}])
        self.cf.problem_ratings[("1", "A")] = 1000
        self.recent = int(time.time()) - 3600  # inside the heatmap's two-year window
        self.cf.status["alice_cf"] = [cf_raw(2, 1, "A", "OK", at=self.recent, name="Theatre Square")]
        db = SessionLocal()
        db.query(models.User).filter_by(username="alice").update({"codeforces_handle": "alice_cf"})
        db.commit()
        db.close()

    async def real_sync(self, item_id):
        from app import sync
        db = SessionLocal()
        try:
            await sync.sync_item(item_id, db)
        finally:
            db.close()

    def test_adding_a_problem_syncs_it_and_fills_the_store(self):
        self.add(self.alice, "codeforces", "problem", "1/A")
        db = SessionLocal()
        try:
            item = db.query(models.AssignmentItem).one()
            self.assertEqual((item.sync_status, item.title, item.rating), ("done", "Theatre Square", 1000))
            self.assertEqual(db.query(models.Submission).count(), 1)
            state = db.query(models.SubmissionSync).one()
            self.assertEqual((state.platform, state.handle, state.submission_count), ("codeforces", "alice_cf", 1))
        finally:
            db.close()
        self.assertIn("solved", self.page())

    def test_heatmap_reads_the_store_and_does_not_hit_the_judge_again(self):
        self.add(self.alice, "codeforces", "problem", "1/A")
        calls_before = self.cf.count("status:")
        r = self.alice.get("/users/alice/activity.json")
        self.assertEqual(r.status_code, 200)
        activity = r.json()
        self.assertEqual(sum(day["cf"] for day in activity.values()), 1)
        self.assertEqual(sum(day["atc"] for day in activity.values()), 0)
        self.assertEqual(self.cf.count("status:"), calls_before)  # served from the store

    def test_heatmap_for_someone_never_synced_loads_their_history(self):
        db = SessionLocal()
        db.query(models.User).filter_by(username="bob").update({"codeforces_handle": "bob_cf"})
        db.commit()
        db.close()
        self.cf.status["bob_cf"] = [cf_raw(5, 9, "A", "OK", at=self.recent), cf_raw(4, 9, "B", "OK", at=self.recent - 100)]
        activity = self.alice.get("/users/bob/activity.json").json()
        self.assertEqual(sum(day["cf"] for day in activity.values()), 2)

    def test_heatmap_survives_a_judge_outage_and_shows_stored_data(self):
        self.add(self.alice, "codeforces", "problem", "1/A")
        db = SessionLocal()
        db.query(models.SubmissionSync).update({"last_synced_at": models.datetime(2020, 1, 1)})
        db.commit()
        db.close()
        self.cf.fail_status.add("alice_cf")
        activity = self.alice.get("/users/alice/activity.json").json()
        self.assertEqual(sum(day["cf"] for day in activity.values()), 1)

    def test_deleting_a_user_removes_their_stored_submissions(self):
        self.add(self.alice, "codeforces", "problem", "1/A")
        db = SessionLocal()
        alice_id = db.query(models.User).filter_by(username="alice").one().id
        db.close()
        r = self.admin.post(f"/admin/users/{alice_id}/delete")
        self.assertEqual(r.status_code, 303)
        db = SessionLocal()
        try:
            for model in (models.Submission, models.SubmissionSync, models.RatingEntry):
                self.assertEqual(db.query(model).filter_by(user_id=alice_id).count(), 0, model.__name__)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
