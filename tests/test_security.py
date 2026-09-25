"""Security behaviour: login redirects and throttling, cookie flags, authorization, URL-path safety."""
import os
import re
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app import auth, models
from app.database import SessionLocal
from app.main import app, _admin_password, _cookie_secure, _secret_key
from app.scraper import atcoder as ac_api
from app.scraper import kilonova as kn_api
from tests.support import reset_db


class SafeNext(unittest.TestCase):
    def test_only_paths_on_this_site_are_kept(self):
        keep = ["/", "/groups/1", "/assignments/3?x=1#item-2", "/users/alice"]
        drop = [None, "", "groups", "//evil.com", "///evil.com", "/\\evil.com", "\\\\evil.com", "https://evil.com",
                "http://evil.com/x", "javascript:alert(1)", "/ok\nSet-Cookie: a=b", "/ok\r\nLocation: x", "/a\\b"]
        for value in keep:
            self.assertEqual(auth.safe_next(value), value, value)
        for value in drop:
            self.assertEqual(auth.safe_next(value), "/", repr(value))


class Throttle(unittest.TestCase):
    def test_locks_after_five_failures_and_backs_off(self):
        t = auth.LoginThrottle()
        for i in range(4):
            t.record_failure("bob", now=100 + i)
        self.assertEqual(t.blocked_for("bob", now=104), 0)        # four failures: still allowed
        t.record_failure("bob", now=104)
        self.assertEqual(t.blocked_for("bob", now=105), 59)       # fifth: locked for 60 s from the last failure
        self.assertEqual(t.blocked_for("bob", now=164), 0)
        for i in range(5):                                        # five more: the lock doubles
            t.record_failure("bob", now=200 + i)
        self.assertEqual(t.blocked_for("bob", now=205), 119)
        self.assertEqual(t.blocked_for("alice", now=205), 0)      # other names are unaffected

    def test_old_failures_expire_and_success_resets(self):
        t = auth.LoginThrottle()
        for i in range(4):
            t.record_failure("bob", now=i)
        t.record_failure("bob", now=1000)                          # the earlier four are outside the window
        self.assertEqual(t.blocked_for("bob", now=1001), 0)
        for i in range(5):
            t.record_failure("carol", now=2000 + i)
        t.reset("carol")
        self.assertEqual(t.blocked_for("carol", now=2005), 0)

    def test_the_lock_is_capped(self):
        t = auth.LoginThrottle()
        for i in range(500):
            t.record_failure("bob", now=i)
        self.assertLessEqual(t.blocked_for("bob", now=500), 3600)


class CookieFlags(unittest.TestCase):
    def test_secure_when_deployed(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("RAILWAY_ENVIRONMENT", None)
            os.environ.pop("COOKIE_SECURE", None)
            self.assertFalse(_cookie_secure())
            os.environ["RAILWAY_ENVIRONMENT"] = "production"
            self.assertTrue(_cookie_secure())
            os.environ.pop("RAILWAY_ENVIRONMENT")
            os.environ["COOKIE_SECURE"] = "1"
            self.assertTrue(_cookie_secure())

    def test_session_cookie_is_httponly_and_samesite(self):
        reset_db()
        with mock.patch("app.scheduler.start"), TestClient(app, follow_redirects=False) as c:
            r = c.post("/login", data={"username": "admin", "password": "test-admin-pw"})
            cookie = r.headers["set-cookie"].lower()
            self.assertIn("httponly", cookie)
            self.assertIn("samesite=lax", cookie)


class NoPublicDefaultsWhenDeployed(unittest.TestCase):
    def env(self, **values):
        base = {k: v for k, v in os.environ.items() if k not in ("RAILWAY_ENVIRONMENT", "SECRET_KEY", "ADMIN_PASSWORD")}
        return mock.patch.dict("os.environ", {**base, **values}, clear=True)

    def test_local_development_may_use_the_defaults(self):
        with self.env():
            self.assertEqual(_secret_key(), "squadforces-dev-secret-change-me")
            self.assertEqual(_admin_password(), "squadforces2024")

    def test_a_deployment_without_them_refuses_to_start(self):
        with self.env(RAILWAY_ENVIRONMENT="production"):
            with self.assertRaises(RuntimeError):
                _secret_key()
            with self.assertRaises(RuntimeError):
                _admin_password()

    def test_configured_values_are_used(self):
        with self.env(RAILWAY_ENVIRONMENT="production", SECRET_KEY="k" * 40, ADMIN_PASSWORD="a-real-password"):
            self.assertEqual((_secret_key(), _admin_password()), ("k" * 40, "a-real-password"))


class WebSecurity(unittest.TestCase):
    def setUp(self):
        reset_db()
        auth.login_throttle.clear()
        p = mock.patch("app.scheduler.start")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch("app.routers.assignments._run_sync", mock.AsyncMock())
        p.start()
        self.addCleanup(p.stop)
        ctx = TestClient(app, follow_redirects=False)
        self.admin = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.admin.post("/login", data={"username": "admin", "password": "test-admin-pw"})
        r = self.admin.post("/groups/new", data={"name": "Team", "hints_allowed": "1"})
        self.gid = int(re.search(r"/groups/(\d+)", r.headers["location"]).group(1))
        r = self.admin.post("/groups/new", data={"name": "Other team"})
        self.gid2 = int(re.search(r"/groups/(\d+)", r.headers["location"]).group(1))
        for name, groups in (("member", [self.gid]), ("member2", [self.gid]), ("outsider", [self.gid2])):
            self.admin.post("/admin/users/new", data={"username": name, "password": "secret-pass1",
                                                       "user_type": "user", "group_ids": groups})
        r = self.admin.post("/assignments/new", data={"group_id": self.gid, "title": "Week"})
        self.aid = int(re.search(r"/assignments/(\d+)", r.headers["location"]).group(1))
        self.admin.post(f"/assignments/{self.aid}/items/add",
                        data={"item_type": "problem", "platform": "cses", "external_id": "1068"})
        db = SessionLocal()
        self.item_id = db.query(models.AssignmentItem).one().id
        self.ids = {u.username: u.id for u in db.query(models.User)}
        db.close()
        self.member, self.member2, self.outsider = self.login("member"), self.login("member2"), self.login("outsider")

    def login(self, name, password="secret-pass1"):
        c = TestClient(app, follow_redirects=False)
        r = c.post("/login", data={"username": name, "password": password})
        self.assertEqual(r.status_code, 303, name)
        return c

    # login -----------------------------------------------------------------

    def test_login_never_redirects_off_site(self):
        for nxt in ("https://evil.com", "//evil.com", "/\\evil.com"):
            r = TestClient(app, follow_redirects=False).post(
                "/login", data={"username": "member", "password": "secret-pass1", "next": nxt})
            self.assertEqual((r.status_code, r.headers["location"]), (303, "/"), nxt)

    def test_login_keeps_a_local_next_and_the_form_never_reflects_a_hostile_one(self):
        r = TestClient(app, follow_redirects=False).post(
            "/login", data={"username": "member", "password": "secret-pass1", "next": f"/assignments/{self.aid}"})
        self.assertEqual(r.headers["location"], f"/assignments/{self.aid}")
        page = TestClient(app).get("/login", params={"next": "https://evil.com/x"}).text
        self.assertNotIn("evil.com", page)

    def test_unauthenticated_redirect_carries_an_encoded_next(self):
        r = TestClient(app, follow_redirects=False).get(f"/assignments/{self.aid}")
        self.assertEqual((r.status_code, r.headers["location"]), (303, f"/login?next=%2Fassignments%2F{self.aid}"))

    def test_repeated_wrong_passwords_are_throttled_even_for_the_right_one(self):
        c = TestClient(app, follow_redirects=False)
        for _ in range(5):
            self.assertEqual(c.post("/login", data={"username": "member2", "password": "wrong-pass"}).status_code, 401)
        r = c.post("/login", data={"username": "member2", "password": "secret-pass1"})
        self.assertEqual(r.status_code, 429)
        self.assertIn("Too many failed attempts", r.text)
        # unknown names get the same treatment, so the throttle reveals nothing about which exist
        for _ in range(5):
            c.post("/login", data={"username": "nobody-here", "password": "x"})
        self.assertEqual(c.post("/login", data={"username": "nobody-here", "password": "x"}).status_code, 429)
        # another account is unaffected
        self.assertEqual(c.post("/login", data={"username": "member", "password": "secret-pass1"}).status_code, 303)

    def test_a_successful_login_clears_the_failures(self):
        c = TestClient(app, follow_redirects=False)
        for _ in range(4):
            c.post("/login", data={"username": "member2", "password": "wrong-pass"})
        self.assertEqual(c.post("/login", data={"username": "member2", "password": "secret-pass1"}).status_code, 303)
        for _ in range(4):
            self.assertEqual(c.post("/login", data={"username": "member2", "password": "wrong-pass"}).status_code, 401)

    def test_passwords_shorter_than_eight_are_refused(self):
        r = TestClient(app).post("/register", data={"username": "shorty", "password": "abc1234", "password2": "abc1234"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("at least 8", r.text)

    # authorization ---------------------------------------------------------

    def test_an_outsider_cannot_see_or_change_another_groups_work(self):
        a, i = self.aid, self.item_id
        checks = [
            ("get", f"/assignments/{a}", {}), ("get", f"/groups/{self.gid}", {}),
            ("post", f"/assignments/{a}/items/add", {"item_type": "problem", "platform": "cses", "external_id": "1"}),
            ("post", f"/assignments/{a}/items/bulk", {"links": "https://cses.fi/problemset/task/1083"}),
            ("post", f"/assignments/{a}/items/{i}/solved", {"user_id": self.ids["outsider"], "solved": "1"}),
            ("post", f"/assignments/{a}/items/{i}/title", {"title": "x"}),
            ("post", f"/assignments/{a}/items/{i}/delete", {}),
            ("post", f"/assignments/{a}/sync", {}), ("post", f"/assignments/{a}/items/{i}/sync", {}),
            ("post", f"/assignments/{a}/hints/add", {"target": f"i{i}", "text": "x"}),
        ]
        for method, url, data in checks:
            r = getattr(self.outsider, method)(url, **({"data": data} if method == "post" else {}))
            self.assertEqual(r.status_code, 403, (method, url))
        db = SessionLocal()
        self.assertEqual(db.query(models.AssignmentItem).count(), 1)
        self.assertEqual(db.query(models.Hint).count(), 0)
        db.close()

    def test_admin_only_actions_are_refused_to_members(self):
        for method, url, data in [
            ("get", "/admin/users", None),
            ("post", "/admin/users/new", {"username": "x", "password": "secret-pass1"}),
            ("post", f"/admin/users/{self.ids['member2']}/delete", {}),
            ("post", f"/admin/users/{self.ids['member2']}/edit", {"username": "member2", "cf_handle": ""}),
            ("post", "/groups/new", {"name": "mine"}), ("post", f"/assignments/{self.aid}/delete", {}),
            ("post", f"/groups/{self.gid}/members/{self.ids['member2']}/remove", {}),
        ]:
            r = getattr(self.member, method)(url, **({"data": data} if data is not None else {}))
            self.assertEqual(r.status_code, 403, (method, url))
        db = SessionLocal()
        self.assertEqual(db.query(models.User).count(), 4)  # admin + 3: nothing created or deleted
        self.assertEqual(db.query(models.Group).count(), 2)
        self.assertEqual(db.query(models.Assignment).count(), 1)
        self.assertEqual(db.query(models.GroupMembership).filter_by(group_id=self.gid).count(), 2)
        db.close()

    def test_a_member_cannot_mark_or_edit_for_someone_else_or_write_hints(self):
        a, i = self.aid, self.item_id
        r = self.member2.post(f"/assignments/{a}/items/{i}/solved", data={"user_id": self.ids["member"], "solved": "1"})
        self.assertEqual(r.status_code, 403)  # only your own progress
        r = self.member.post(f"/assignments/{a}/hints/add", data={"target": f"i{i}", "text": "answer", "is_solution": "1"})
        self.assertEqual(r.status_code, 303)
        db = SessionLocal()
        hint = db.query(models.Hint).one()
        self.assertEqual((hint.kind, hint.author_id), ("note", self.ids["member"]))  # a member can only leave a note
        hint_id = hint.id
        db.close()
        self.assertEqual(self.member2.post(f"/assignments/{a}/hints/{hint_id}/edit", data={"text": "hijack"}).status_code, 403)
        self.assertEqual(self.member2.post(f"/assignments/{a}/hints/{hint_id}/delete").status_code, 403)

    def test_a_member_cannot_edit_another_users_profile_or_reload_their_history(self):
        r = self.member.post("/users/member2/edit", data={"full_name": "pwned", "cf_handle": "",
                                                           "atcoder_handle": "", "kilonova_handle": ""})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.member.post("/users/member2/reload-history").status_code, 403)

    def test_editing_the_admin_account_never_demotes_it(self):
        r = self.admin.post("/admin/users/0/edit", data={"username": "admin", "user_type": "user", "cf_handle": ""})
        self.assertEqual(r.status_code, 303)
        db = SessionLocal()
        self.assertEqual(db.get(models.User, 0).user_type, "admin")
        db.close()
        self.assertEqual(self.admin.get("/admin/users").status_code, 200)

    def test_the_admin_account_cannot_be_deleted(self):
        r = self.admin.post("/admin/users/0/delete")
        self.assertIn("Cannot+delete+admin", r.headers["location"])


class UrlPathSafety(unittest.IsolatedAsyncioTestCase):
    """A handle is user input that ends up in a URL path; it must stay one path segment."""

    async def test_atcoder_and_kilonova_handles_are_encoded(self):
        seen = []

        class FakeResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"status": "success", "data": {"id": 1}} if "kilonova" in seen[-1] else []

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params=None):
                seen.append(url)
                return FakeResp()

        with mock.patch.object(ac_api.httpx, "AsyncClient", FakeClient), \
                mock.patch.object(kn_api.httpx, "AsyncClient", FakeClient):
            await ac_api.get_rating_history("../../evil?x=1#y")
            await kn_api.validate_handle("../problem/1/../../x")
        for url in seen:
            path = url.split("://", 1)[1].split("/", 1)[1]
            self.assertNotIn("../", path)
            self.assertNotIn("?", path)
        self.assertEqual(seen[0], "https://atcoder.jp/users/..%2F..%2Fevil%3Fx%3D1%23y/history/json")
        self.assertTrue(seen[1].startswith("https://kilonova.ro/api/user/byName/..%2Fproblem%2F1"))


if __name__ == "__main__":
    unittest.main()
