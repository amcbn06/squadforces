"""Invite links, closed registration, group ownership, quotas and member limits, and the migration of old groups."""
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from fastapi.testclient import TestClient

from app import auth, invites, models
from app.database import SessionLocal
from app.limits import MAX_GROUPS_PER_USER, MAX_GROUP_MEMBERS
from app.main import app
from tests.support import DbTestCase, reset_db


# ── the service ────────────────────────────────────────────────────────────────────────────────

class InviteService(DbTestCase):
    def setUp(self):
        super().setUp()
        self.admin = models.User(id=0, username="admin", password_hash="x", user_type="admin")
        self.db.add(self.admin)
        self.db.commit()
        self.owner = self.user("owner")
        self.group_, _ = self.group([self.owner])

    def test_the_token_is_never_stored_and_can_be_found_by_link_or_code(self):
        inv, token = invites.create(self.db, created_by=self.admin)
        self.assertNotIn(token, (inv.token_hash, inv.token_hint, inv.label or ""))
        self.assertEqual(inv.token_hash, invites.hash_token(token))
        self.assertEqual(inv.token_hint, token[-4:])
        for pasted in (token, f"  {token}\n", f"https://example.com/invite/{token}", f"http://localhost:8000/invite/{token}/"):
            self.assertEqual(invites.find(self.db, pasted).id, inv.id, pasted)
        self.assertIsNone(invites.find(self.db, "not-a-token"))
        self.assertIsNone(invites.find(self.db, ""))
        cols = [c.name for c in models.Invite.__table__.columns]
        self.assertNotIn("token", cols)

    def test_tokens_are_long_and_unique(self):
        tokens = {invites.create(self.db, created_by=self.admin)[1] for _ in range(30)}
        self.assertEqual(len(tokens), 30)
        self.assertTrue(all(len(t) >= 30 for t in tokens))

    def test_days_and_uses_are_clamped(self):
        inv, _ = invites.create(self.db, created_by=self.admin, days=9999, max_uses=10**6)
        self.assertLessEqual((inv.expires_at - datetime.utcnow()).days, 30)
        self.assertEqual(inv.max_uses, 100)
        inv, _ = invites.create(self.db, created_by=self.admin, days=-5, max_uses=0)
        self.assertEqual(inv.max_uses, 1)
        self.assertGreater(inv.expires_at, datetime.utcnow())

    def test_platform_and_group_invites_have_the_same_shape(self):
        platform, _ = invites.create(self.db, created_by=self.admin)
        grp, _ = invites.create(self.db, created_by=self.owner, group=self.group_)
        self.assertEqual((platform.group_id, platform.allows_signup), (None, True))
        self.assertEqual((grp.group_id, grp.allows_signup), (self.group_.id, False))
        signup_grp, _ = invites.create(self.db, created_by=self.admin, group=self.group_, allows_signup=True)
        self.assertTrue(signup_grp.allows_signup)

    def test_status_transitions(self):
        inv, _ = invites.create(self.db, created_by=self.admin, max_uses=2)
        self.assertEqual(invites.status(inv), "active")
        self.assertIsNone(invites.why_unusable(inv))
        self.assertTrue(invites.redeem(self.db, inv))
        self.db.commit()
        self.assertTrue(invites.redeem(self.db, inv))
        self.db.commit()
        self.db.refresh(inv)
        self.assertEqual((inv.uses, invites.status(inv)), (2, "used up"))
        self.assertFalse(invites.redeem(self.db, inv))            # a third use is refused
        self.assertIn("used up", invites.why_unusable(inv))

        inv2, _ = invites.create(self.db, created_by=self.admin)
        invites.revoke(self.db, inv2)
        self.assertEqual(invites.status(inv2), "revoked")
        self.assertFalse(invites.redeem(self.db, inv2))

        inv3, _ = invites.create(self.db, created_by=self.admin)
        inv3.expires_at = datetime.utcnow() - timedelta(seconds=1)
        self.db.commit()
        self.assertEqual(invites.status(inv3), "expired")
        self.assertFalse(invites.redeem(self.db, inv3))
        self.assertEqual(invites.why_unusable(None), "This invite link isn't valid.")

    def test_join_group_rules(self):
        alice, bob, carol = self.user("alice"), self.user("bob"), self.user("carol")
        self.group_.max_members = 3                                # owner + 2 more
        self.db.commit()
        self.assertIsNone(invites.join_group(self.db, self.group_, alice))
        self.db.commit()
        self.assertIn("already in", invites.join_group(self.db, self.group_, alice))
        self.assertIsNone(invites.join_group(self.db, self.group_, bob))
        self.db.commit()
        self.assertIn("is full", invites.join_group(self.db, self.group_, carol))
        self.assertIn("can't be a group member", invites.join_group(self.db, self.group_, self.admin))

    def test_absolute_url_prefers_public_settings(self):
        class Req:
            base_url = "http://internal:8080/"
        with mock.patch.dict(os.environ, {"PUBLIC_URL": "", "RAILWAY_PUBLIC_DOMAIN": ""}):
            self.assertEqual(invites.absolute_url(Req, "/invite/x"), "http://internal:8080/invite/x")
        with mock.patch.dict(os.environ, {"PUBLIC_URL": "", "RAILWAY_PUBLIC_DOMAIN": "app.up.railway.app"}):
            self.assertEqual(invites.absolute_url(Req, "/invite/x"), "https://app.up.railway.app/invite/x")
        with mock.patch.dict(os.environ, {"PUBLIC_URL": "https://squad.example/", "RAILWAY_PUBLIC_DOMAIN": "a.b"}):
            self.assertEqual(invites.absolute_url(Req, "/invite/x"), "https://squad.example/invite/x")

    def test_open_registration_is_off_unless_asked_for(self):
        with mock.patch.dict(os.environ, {"ALLOW_OPEN_REGISTRATION": ""}):
            self.assertFalse(invites.open_registration())
        for v in ("1", "true", "YES"):
            with mock.patch.dict(os.environ, {"ALLOW_OPEN_REGISTRATION": v}):
                self.assertTrue(invites.open_registration())


# ── migrating groups that predate ownership ─────────────────────────────────────────────────────

class GroupMigration(DbTestCase):
    def test_old_groups_get_the_admin_as_owner_and_a_limit_and_lose_nothing(self):
        from app.database import migrate_groups
        users = [self.user(f"u{i}") for i in range(12)]
        big, big_assignment = self.group(users, name="Big")            # 12 members: over the default limit
        small, small_assignment = self.group(users[:2], name="Small")
        item = self.item(small_assignment, "cses", "problem", "1068")
        self.db.add(models.Result(assignment_item_id=item.id, user_id=users[0].id, solved=True))
        self.db.commit()
        # what a database from before this feature looks like: no owner, no limit
        self.db.query(models.Group).update({"owner_id": None, "max_members": None})
        self.db.commit()

        migrate_groups()

        self.db.expire_all()
        g_small, g_big = self.db.get(models.Group, small.id), self.db.get(models.Group, big.id)
        self.assertEqual((g_small.owner_id, g_small.max_members), (0, 10))
        self.assertEqual((g_big.owner_id, g_big.max_members), (0, 12))   # never below the current size
        # nothing was removed or moved
        self.assertEqual(self.db.query(models.GroupMembership).filter_by(group_id=big.id).count(), 12)
        self.assertEqual(self.db.query(models.GroupMembership).filter_by(group_id=small.id).count(), 2)
        self.assertEqual(self.db.query(models.Assignment).count(), 2)
        self.assertTrue(self.db.query(models.Result).one().solved)

    def test_running_it_again_or_on_migrated_groups_changes_nothing(self):
        from app.database import migrate_groups
        u = self.user("owner")
        g, _ = self.group([u])
        g.owner_id, g.max_members = u.id, 4
        self.db.commit()
        migrate_groups()
        migrate_groups()
        self.db.expire_all()
        g = self.db.get(models.Group, g.id)
        self.assertEqual((g.owner_id, g.max_members), (u.id, 4))

    def test_the_real_upgrade_path_on_an_old_database_file(self):
        """A database created before owner_id / max_members existed, upgraded the way startup does it."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db").replace("\\", "/")
            con = sqlite3.connect(path)
            con.executescript("""
                CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(50), password_hash VARCHAR(200));
                CREATE TABLE groups (id INTEGER PRIMARY KEY, name VARCHAR(100), description TEXT,
                                     hints_allowed BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME);
                CREATE TABLE group_memberships (id INTEGER PRIMARY KEY, group_id INTEGER, user_id INTEGER, joined_at DATETIME);
                INSERT INTO users VALUES (0, 'admin', 'x'), (1, 'a', 'y'), (2, 'b', 'z');
                INSERT INTO groups (id, name, hints_allowed) VALUES (1, 'Old group', 0), (2, 'Hinting group', 1);
                INSERT INTO group_memberships (group_id, user_id) VALUES (1, 1), (1, 2);
            """)
            con.commit()
            con.close()
            code = ("from app.database import ensure_columns, migrate_groups, engine\n"
                    "from sqlalchemy import text\n"
                    "ensure_columns(); migrate_groups(); ensure_columns(); migrate_groups()\n"
                    "with engine.connect() as c:\n"
                    "    print(c.execute(text('SELECT id, name, owner_id, max_members, hints_allowed, notes_allowed FROM groups')).all())\n"
                    "    print(c.execute(text('SELECT COUNT(*) FROM group_memberships')).scalar())\n")
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                 env={**os.environ, "DATABASE_URL": "sqlite:///" + path}, cwd=os.getcwd())
            self.assertEqual(out.returncode, 0, out.stderr[-500:])
            lines = out.stdout.strip().splitlines()
            # a group that had hints on keeps notes on; one that had neither still has neither
            self.assertEqual(lines[0], "[(1, 'Old group', 0, 10, 0, 0), (2, 'Hinting group', 0, 10, 1, 1)]")
            self.assertEqual(lines[1], "2")


# ── the site ────────────────────────────────────────────────────────────────────────────────────

class SiteTestCase(unittest.TestCase):
    def setUp(self):
        reset_db()
        auth.login_throttle.clear()
        p = mock.patch("app.auth.PBKDF2_ITERATIONS", 1000)  # the tests create many accounts; keep hashing cheap
        p.start()
        self.addCleanup(p.stop)
        for target in ("app.scheduler.start", "app.histories.load_histories"):
            p = mock.patch(target, mock.AsyncMock() if "histories" in target else mock.DEFAULT)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("app.routers.assignments._run_sync", mock.AsyncMock())
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"ALLOW_OPEN_REGISTRATION": ""})   # the real setting: closed
        p.start()
        self.addCleanup(p.stop)
        ctx = TestClient(app, follow_redirects=False)
        self.admin = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.admin.post("/login", data={"username": "admin", "password": "test-admin-pw"})

    def make_user(self, name, user_type="user", groups=()):
        self.admin.post("/admin/users/new", data={"username": name, "password": "secret-pass1",
                                                   "user_type": user_type, "group_ids": list(groups)})
        return self.login(name)

    def login(self, name):
        c = TestClient(app, follow_redirects=False)
        r = c.post("/login", data={"username": name, "password": "secret-pass1"})
        self.assertEqual(r.status_code, 303, name)
        return c

    def new_group(self, client, name="G", max_members=10, hints=False, notes=None):
        data = {"name": name, "max_members": max_members}
        if hints:
            data["hints_allowed"] = "1"
        if hints if notes is None else notes:
            data["notes_allowed"] = "1"
        r = client.post("/groups/new", data=data)
        m = re.search(r"/groups/(\d+)", r.headers.get("location", ""))
        return (int(m.group(1)) if m else None), r

    def group_row(self, gid):
        db = SessionLocal()
        try:
            g = db.get(models.Group, gid)
            return None if g is None else (g.owner_id, g.max_members, sorted(m.user.username for m in g.memberships))
        finally:
            db.close()

    def uid(self, name):
        db = SessionLocal()
        try:
            return db.query(models.User).filter_by(username=name).one().id
        finally:
            db.close()

    def make_invite(self, client, gid, **data):
        r = client.post(f"/groups/{gid}/invites", data=data)
        self.assertEqual(r.status_code, 303, r.text[:200])
        page = client.get(f"/groups/{gid}").text
        m = re.search(r'value="(https?://[^"]+/invite/([^"]+))" style', page)
        self.assertIsNotNone(m, "the new link should be shown")
        return m.group(2)

    def make_platform_invite(self, **data):
        self.admin.post("/admin/invites/new", data=data)
        page = self.admin.get("/admin/invites").text
        m = re.search(r'value="(https?://[^"]+/invite/([^"]+))" style', page)
        self.assertIsNotNone(m)
        return m.group(2)


class ClosedRegistration(SiteTestCase):
    def register(self, client, name, token="", **extra):
        data = {"username": name, "password": "secret-pass1", "password2": "secret-pass1", "invite": token,
                "cf_handle": "", "atcoder_handle": "", "kilonova_handle": ""}
        data.update(extra)
        return client.post("/register", data=data)

    def count_users(self):
        db = SessionLocal()
        try:
            return db.query(models.User).count()
        finally:
            db.close()

    def test_no_invite_no_account(self):
        c = TestClient(app, follow_redirects=False)
        page = c.get("/register")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Registration is by invitation", page.text)
        self.assertNotIn('name="password"', page.text)             # no sign-up form at all
        r = self.register(c, "sneaky")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.count_users(), 1)                       # just the admin

    def test_bad_links_are_refused_with_a_reason(self):
        c = TestClient(app, follow_redirects=False)
        self.assertEqual(self.register(c, "x", "totally-made-up").status_code, 403)
        self.assertEqual(c.get("/register", params={"invite": "totally-made-up"}).status_code, 403)
        for how, expect in (("revoke", "revoked"), ("expire", "expired"), ("use", "used up")):
            token = self.make_platform_invite(max_uses=1)
            db = SessionLocal()
            inv = invites.find(db, token)
            if how == "revoke":
                inv.revoked_at = datetime.utcnow()
            elif how == "expire":
                inv.expires_at = datetime.utcnow() - timedelta(minutes=1)
            else:
                inv.uses = inv.max_uses
            db.commit()
            db.close()
            r = self.register(c, f"user-{how}", token)
            self.assertEqual(r.status_code, 403, how)
            self.assertIn(expect, r.text)
        self.assertEqual(self.count_users(), 1)

    def test_a_platform_invite_creates_the_account_and_is_used_up(self):
        token = self.make_platform_invite(max_uses=1, label="cohort")
        c = TestClient(app, follow_redirects=False)
        page = c.get("/register", params={"invite": token})
        self.assertEqual(page.status_code, 200)
        self.assertIn('name="invite" value="' + token, page.text)
        self.assertIn("invited to create an account", page.text)
        r = self.register(c, "newbie", token)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/"))
        self.assertEqual(c.get("/groups/").status_code, 200)         # signed in
        db = SessionLocal()
        self.assertEqual(db.query(models.User).filter_by(username="newbie").one().user_type, "user")
        self.assertEqual(invites.find(db, token).uses, 1)
        db.close()
        # the single use is gone: a second person can't use the same link
        self.assertEqual(self.register(TestClient(app, follow_redirects=False), "second", token).status_code, 403)
        self.assertEqual(self.count_users(), 2)

    def test_a_multi_use_invite_and_pasting_the_whole_link(self):
        token = self.make_platform_invite(max_uses=3)
        for i in range(3):
            r = self.register(TestClient(app, follow_redirects=False), f"p{i}", f"https://squad.example/invite/{token}")
            self.assertEqual(r.status_code, 303, i)
        self.assertEqual(self.register(TestClient(app, follow_redirects=False), "p3", token).status_code, 403)

    def test_the_invite_decides_the_account_type(self):
        token = self.make_platform_invite(user_type="student")
        self.register(TestClient(app, follow_redirects=False), "pupil", token)
        db = SessionLocal()
        self.assertEqual(db.query(models.User).filter_by(username="pupil").one().user_type, "student")
        db.close()

    def test_a_failed_registration_does_not_burn_the_invite(self):
        token = self.make_platform_invite(max_uses=1)
        c = TestClient(app, follow_redirects=False)
        r = self.register(c, "x", token, password2="different")     # validation fails before anything is created
        self.assertEqual(r.status_code, 422)
        self.assertIn('name="invite" value="' + token, r.text)      # the form keeps the token
        db = SessionLocal()
        self.assertEqual(invites.find(db, token).uses, 0)
        db.close()
        self.assertEqual(self.register(c, "x", token).status_code, 303)

    def test_a_platform_invite_can_add_the_new_account_to_a_group(self):
        gid, _ = self.new_group(self.admin, "Cohort")
        token = self.make_platform_invite(group_id=gid, max_uses=2)
        r = self.register(TestClient(app, follow_redirects=False), "joiner", token)
        self.assertEqual(r.headers["location"], f"/groups/{gid}")
        self.assertEqual(self.group_row(gid)[2], ["joiner"])

    def test_a_full_group_still_gets_the_account_but_not_the_membership(self):
        gid, _ = self.new_group(self.admin, "Tiny", max_members=1)
        token = self.make_platform_invite(group_id=gid, max_uses=3)
        self.register(TestClient(app, follow_redirects=False), "first", token)
        c = TestClient(app, follow_redirects=False)
        r = self.register(c, "second", token)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.group_row(gid)[2], ["first"])
        self.assertEqual(self.count_users(), 3)
        self.assertIn("couldn't join the group", html.unescape(c.get("/groups/").text))   # they are told, once
        self.assertNotIn("couldn't join the group", html.unescape(c.get("/groups/").text))

    def test_a_group_invite_does_not_create_accounts_unless_the_admin_says_so(self):
        owner = self.make_user("owner")
        gid, _ = self.new_group(owner, "Mine")
        token = self.make_invite(owner, gid)
        c = TestClient(app, follow_redirects=False)
        r = self.register(c, "outsider", token)
        self.assertEqual(r.status_code, 403)
        self.assertIn("joining a group", html.unescape(r.text))
        self.assertEqual(c.get("/register", params={"invite": token}).status_code, 403)
        # the owner can't turn signup on, even by asking
        token2 = self.make_invite(owner, gid, allows_signup="1")
        self.assertEqual(self.register(TestClient(app, follow_redirects=False), "still-no", token2).status_code, 403)
        # the admin can
        self.admin.post(f"/groups/{gid}/invites", data={"allows_signup": "1", "max_uses": 2})
        page = self.admin.get(f"/groups/{gid}").text
        token3 = re.search(r'value="https?://[^"]+/invite/([^"]+)" style', page).group(1)
        r = self.register(TestClient(app, follow_redirects=False), "welcome", token3)
        self.assertEqual(r.headers["location"], f"/groups/{gid}")
        self.assertIn("welcome", self.group_row(gid)[2])

    def test_open_registration_still_works_when_switched_on(self):
        with mock.patch.dict(os.environ, {"ALLOW_OPEN_REGISTRATION": "1"}):
            c = TestClient(app, follow_redirects=False)
            self.assertIn('name="password"', c.get("/register").text)
            self.assertEqual(self.register(c, "walkin").status_code, 303)
            # ...but a link that was given is still checked
            self.assertEqual(self.register(TestClient(app, follow_redirects=False), "bad", "nonsense").status_code, 403)

    def test_a_platform_link_opened_as_an_invite_goes_to_registration(self):
        token = self.make_platform_invite()
        r = TestClient(app, follow_redirects=False).get(f"/invite/{token}")
        self.assertEqual((r.status_code, r.headers["location"]), (303, f"/register?invite={token}"))
        # someone who already has an account is told so instead
        page = self.make_user("existing").get(f"/invite/{token}")
        self.assertIn("already have an account", page.text)


class GroupCreation(SiteTestCase):
    def test_a_user_creates_a_group_and_owns_it(self):
        c = self.make_user("ana")
        gid, r = self.new_group(c, "Ana's team", max_members=6)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.group_row(gid), (self.uid("ana"), 6, ["ana"]))       # owner, limit, first member
        page = c.get("/groups/").text
        self.assertIn("(yours)", page)
        self.assertIn("You own 1 of 5 groups", page)

    def test_at_most_five_groups_per_user(self):
        c = self.make_user("ana")
        for i in range(MAX_GROUPS_PER_USER):
            self.assertIsNotNone(self.new_group(c, f"g{i}")[0], i)
        gid, r = self.new_group(c, "one too many")
        self.assertEqual((gid, r.status_code), (None, 403))
        page = c.get("/groups/").text
        self.assertIn("You own 5 of 5 groups", page)
        self.assertNotIn("+ New group", page)
        r = c.get("/groups/new")                                    # the form isn't offered either
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/groups/"))
        self.assertIn("already own 5", html.unescape(c.get("/groups/").text))
        # another user has their own allowance; deleting one frees a slot
        self.assertIsNotNone(self.new_group(self.make_user("bo"), "bo's")[0])
        db = SessionLocal()
        first = db.query(models.Group).filter_by(owner_id=self.uid("ana")).first().id
        db.close()
        self.assertEqual(c.post(f"/groups/{first}/delete").status_code, 303)
        self.assertIsNotNone(self.new_group(c, "back to five")[0])

    def test_students_cannot_create_groups(self):
        c = self.make_user("pupil", user_type="student")
        r = c.get("/groups/new")
        self.assertEqual(r.status_code, 303)
        self.assertIn("Student accounts can't create groups", html.unescape(c.get("/groups/").text))
        gid, r = self.new_group(c, "nope")
        self.assertEqual((gid, r.status_code), (None, 403))
        self.assertNotIn("+ New group", c.get("/groups/").text)

    def test_the_admin_is_not_limited_and_owns_what_it_creates(self):
        for i in range(MAX_GROUPS_PER_USER + 2):
            gid, _ = self.new_group(self.admin, f"a{i}", max_members=50)
            self.assertIsNotNone(gid)
        self.assertEqual(self.group_row(gid), (0, 50, []))          # the admin is not a member of what it creates

    def test_member_limits_are_validated(self):
        c = self.make_user("ana")
        for bad in (0, -1, MAX_GROUP_MEMBERS + 1, 500):
            gid, r = self.new_group(c, "x", max_members=bad)
            self.assertEqual((gid, r.status_code), (None, 422), bad)
        self.assertIsNotNone(self.new_group(c, "ok", max_members=MAX_GROUP_MEMBERS)[0])
        self.assertIsNotNone(self.new_group(c, "ok1", max_members=1)[0])
        self.assertEqual(self.new_group(c, "  ", max_members=5)[1].status_code, 422)   # a name is still required

    def test_the_limit_cannot_go_below_the_current_size(self):
        owner = self.make_user("owner")
        gid, _ = self.new_group(owner, "G")
        for name in ("m1", "m2"):
            self.admin.post(f"/groups/{gid}/members/add", data={"member_username": name}) if False else None
        self.make_user("m1", groups=[gid])
        self.make_user("m2", groups=[gid])
        r = owner.post(f"/groups/{gid}/edit", data={"name": "G", "max_members": 2})
        self.assertEqual(r.status_code, 422)                         # owner + m1 + m2 = 3
        self.assertIn("already has 3 members", r.text)
        self.assertEqual(owner.post(f"/groups/{gid}/edit", data={"name": "G2", "max_members": 3}).status_code, 303)
        self.assertEqual(self.group_row(gid)[1], 3)


class Ownership(SiteTestCase):
    def setUp(self):
        super().setUp()
        self.owner = self.make_user("owner")
        self.gid, _ = self.new_group(self.owner, "Team", hints=True)
        self.member = self.make_user("member", groups=[self.gid])
        self.other = self.make_user("other")
        self.other_gid, _ = self.new_group(self.other, "Other's")
        r = self.owner.post("/assignments/new", data={"group_id": self.gid, "title": "Week"})
        self.aid = int(re.search(r"/assignments/(\d+)", r.headers["location"]).group(1))
        self.owner.post(f"/assignments/{self.aid}/items/add", data={"item_type": "problem", "platform": "cses", "external_id": "1068"})
        db = SessionLocal()
        self.item_id = db.query(models.AssignmentItem).one().id
        db.close()

    def test_the_new_assignment_form_suggests_the_next_number_and_todays_date(self):
        from datetime import date
        page = self.owner.get(f"/assignments/new?group_id={self.gid}").text
        self.assertIn('value="Assignment 2"', page)                        # setUp already made one
        self.assertIn(f'value="{date.today().isoformat()}"', page)
        self.owner.post("/assignments/new", data={"group_id": self.gid, "title": "Assignment 2"})
        self.assertIn('value="Assignment 3"', self.owner.get(f"/assignments/new?group_id={self.gid}").text)

    def test_a_group_page_lists_dated_and_undated_assignments_together(self):
        """The date is optional; a group mixing both used to answer 500."""
        for title, day in (("Undated old", ""), ("Week B", "2026-05-11"), ("Week A", "2026-05-04"), ("Undated new", "")):
            r = self.owner.post("/assignments/new", data={"group_id": self.gid, "title": title, "week_start_date": day})
            self.assertEqual(r.status_code, 303, title)
        r = self.owner.get(f"/groups/{self.gid}")
        self.assertEqual(r.status_code, 200)
        page = r.text
        order = [page.index(t) for t in ("Week B", "Week A", "Undated new", "Undated old")]
        self.assertEqual(order, sorted(order))                # dated newest first, then undated newest first

    def test_the_owner_manages_their_group(self):
        page = self.owner.get(f"/groups/{self.gid}").text
        for text in ("Edit", "Delete", "Invite links", "Owner: owner"):
            self.assertIn(text, page)
        self.assertEqual(self.owner.post(f"/groups/{self.gid}/edit", data={"name": "Renamed", "max_members": 8}).status_code, 303)
        self.assertEqual(self.group_row(self.gid)[1], 8)
        # remove a member (not themselves)
        self.assertEqual(self.owner.post(f"/groups/{self.gid}/members/{self.uid('member')}/remove").status_code, 303)
        self.assertEqual(self.group_row(self.gid)[2], ["owner"])
        self.assertEqual(self.owner.post(f"/groups/{self.gid}/members/{self.uid('owner')}/remove").status_code, 400)

    def test_members_and_other_owners_cannot_manage_it(self):
        for client, who in ((self.member, "member"), (self.other, "other owner")):
            for method, url, data in [
                ("get", f"/groups/{self.gid}/edit-page", None),
                ("post", f"/groups/{self.gid}/edit", {"name": "hijack", "max_members": 5}),
                ("post", f"/groups/{self.gid}/delete", None),
                ("post", f"/groups/{self.gid}/members/{self.uid('owner')}/remove", None),
                ("post", f"/groups/{self.gid}/invites", {}),
                ("post", f"/assignments/{self.aid}/delete", None),
            ]:
                r = getattr(client, method)(url, **({"data": data} if data is not None else {}))
                self.assertEqual(r.status_code, 403, (who, method, url))
        self.assertEqual(self.group_row(self.gid)[0:1], (self.uid("owner"),))
        page = self.member.get(f"/groups/{self.gid}").text
        self.assertNotIn("Invite links", page)                      # the panel isn't even rendered for members
        self.assertNotIn(">Delete<", page)

    def test_nobody_can_add_people_by_username_except_the_admin(self):
        for client in (self.owner, self.member):
            r = client.post(f"/groups/{self.gid}/members/add", data={"member_username": "other"})
            self.assertEqual(r.status_code, 403)
        self.assertNotIn("other", self.group_row(self.gid)[2])
        # the admin can, whatever the limit says
        self.owner.post(f"/groups/{self.gid}/edit", data={"name": "Team", "max_members": 2})   # owner + member: full
        r = self.admin.post(f"/groups/{self.gid}/members/add", data={"member_username": "other"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.group_row(self.gid)[2], ["member", "other", "owner"])           # over the limit of 2

    def test_the_owner_writes_hints_and_edits_any_note_in_their_group(self):
        i = self.item_id
        r = self.owner.post(f"/assignments/{self.aid}/hints/add", data={"target": f"i{i}", "text": "think DP", "is_solution": "1"})
        self.assertEqual(r.status_code, 303)
        self.member.post(f"/assignments/{self.aid}/hints/add", data={"target": f"i{i}", "text": "took 40 min", "time_minutes": "40"})
        db = SessionLocal()
        kinds = sorted(h.kind for h in db.query(models.Hint))
        note_id = db.query(models.Hint).filter_by(kind="note").one().id
        db.close()
        self.assertEqual(kinds, ["note", "solution"])              # the owner's entry is a solution, the member's a note
        self.assertEqual(self.owner.post(f"/assignments/{self.aid}/hints/{note_id}/edit", data={"text": "cleaned up"}).status_code, 303)
        self.assertEqual(self.owner.post(f"/assignments/{self.aid}/hints/{note_id}/delete").status_code, 303)
        # another group's owner has no say here
        r = self.other.post(f"/assignments/{self.aid}/hints/add", data={"target": f"i{i}", "text": "x"})
        self.assertEqual(r.status_code, 403)

    def hints(self):
        db = SessionLocal()
        try:
            return [(h.kind, h.author_id, h.time_minutes, h.text) for h in db.query(models.Hint).order_by(models.Hint.id)]
        finally:
            db.close()

    def test_an_owner_can_leave_a_note_a_hint_or_a_solution_each_on_purpose(self):
        i, owner_id = self.item_id, self.uid("owner")
        add = lambda **data: self.owner.post(f"/assignments/{self.aid}/hints/add", data={"target": f"i{i}", **data})
        self.assertEqual(add(text="my note", kind="note", time_minutes="25").status_code, 303)
        self.assertEqual(add(text="a hint", kind="hint").status_code, 303)
        self.assertEqual(add(text="the answer", kind="hint", is_solution="1").status_code, 303)
        self.assertEqual(add(text="solution via kind", kind="solution").status_code, 303)
        self.assertEqual(add(text="older form, plain").status_code, 303)                       # no kind: a hint, as before
        self.assertEqual(add(text="older form, ticked", is_solution="1").status_code, 303)     # no kind: a solution, as before
        self.assertEqual(self.hints(), [
            ("note", owner_id, 25, "my note"),                    # a note has its author and the time it took
            ("hint", None, None, "a hint"),
            ("solution", None, None, "the answer"),
            ("solution", None, None, "solution via kind"),
            ("hint", None, None, "older form, plain"),
            ("solution", None, None, "older form, ticked"),
        ])

    def switch(self, hints, notes):
        data = {"name": "Team", "max_members": 10}
        if hints:
            data["hints_allowed"] = "1"
        if notes:
            data["notes_allowed"] = "1"
        self.assertEqual(self.owner.post(f"/groups/{self.gid}/edit", data=data).status_code, 303)

    def test_hints_and_notes_are_switched_on_and_off_separately(self):
        i, aid = self.item_id, self.aid
        post = lambda client, **d: client.post(f"/assignments/{aid}/hints/add", data={"target": f"i{i}", "text": "t", **d}).status_code
        self.assertEqual(post(self.owner, kind="hint"), 303)
        self.assertEqual(post(self.member, time_minutes="5"), 303)
        # hints off, notes on: the note stays visible and can be added, hints can't be
        self.switch(hints=False, notes=True)
        page = self.member.get(f"/assignments/{aid}").text
        self.assertIn("var NOTES_ON = true;", page)
        self.assertIn("var HINTS_ON = false;", page)
        self.assertRegex(page, r'"kind": ?"note"')
        self.assertNotRegex(page, r'"kind": ?"hint"')                # the existing hint is hidden, not deleted
        self.assertEqual(post(self.owner, kind="hint"), 400)
        self.assertEqual(post(self.owner, kind="note"), 303)
        self.assertEqual(post(self.member), 303)
        # notes off, hints on
        self.switch(hints=True, notes=False)
        page = self.member.get(f"/assignments/{aid}").text
        self.assertRegex(page, r'"kind": ?"hint"')
        self.assertNotRegex(page, r'"kind": ?"note"')
        self.assertEqual(post(self.member), 400)
        self.assertEqual(post(self.owner, kind="note"), 400)
        self.assertEqual(post(self.owner, kind="hint"), 303)
        # both off: no button, nothing accepted
        self.switch(hints=False, notes=False)
        self.assertNotIn("openHints(this)", self.member.get(f"/assignments/{aid}").text)
        self.assertEqual(post(self.owner, kind="hint"), 400)
        self.assertEqual(post(self.member), 400)
        self.assertEqual(len(self.hints()), 5)                        # switching things off never deletes anything

    def test_the_group_form_has_both_switches(self):
        self.switch(hints=False, notes=True)
        page = self.owner.get(f"/groups/{self.gid}/edit-page").text
        self.assertRegex(page, r'name="notes_allowed" value="1"\s*checked')
        self.assertNotRegex(page, r'name="hints_allowed" value="1"\s*checked')

    def test_a_managers_note_needs_a_valid_time_like_anyone_elses(self):
        r = self.owner.post(f"/assignments/{self.aid}/hints/add", data={"target": f"i{self.item_id}", "text": "n", "kind": "note", "time_minutes": "soon"})
        self.assertEqual(r.status_code, 400)
        r = self.owner.post(f"/assignments/{self.aid}/hints/add", data={"target": f"i{self.item_id}", "text": "n", "kind": "note"})
        self.assertEqual(r.status_code, 303)                                                    # the time is optional
        self.assertEqual(self.hints(), [("note", self.uid("owner"), None, "n")])

    def test_the_admin_can_leave_notes_too(self):
        r = self.admin.post(f"/assignments/{self.aid}/hints/add", data={"target": f"i{self.item_id}", "text": "from admin", "kind": "note", "time_minutes": "5"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.hints(), [("note", 0, 5, "from admin")])

    def test_a_member_can_only_ever_leave_notes_whatever_they_post(self):
        for data in ({"kind": "hint"}, {"kind": "solution"}, {"is_solution": "1"}, {"kind": "hint", "is_solution": "1"}, {}):
            r = self.member.post(f"/assignments/{self.aid}/hints/add", data={"target": f"i{self.item_id}", "text": "sneaky", **data})
            self.assertEqual(r.status_code, 303)
        kinds = {h[0] for h in self.hints()}
        authors = {h[1] for h in self.hints()}
        self.assertEqual((kinds, authors), ({"note"}, {self.uid("member")}))

    def test_the_page_offers_managers_two_buttons_and_members_one(self):
        for client, add_hint in ((self.owner, True), (self.admin, True), (self.member, False)):
            page = client.get(f"/assignments/{self.aid}").text
            self.assertEqual("var HINT_ADMIN = true;" in page, add_hint)
            self.assertIn("'+ Add note'", page)                                               # everyone can leave a note
            self.assertIn("'+ Add hint'", page)                                               # (only offered when HINT_ADMIN)
        self.assertRegex(page, r"if \(HINT_ADMIN && HINTS_ON\) row\.appendChild\(addButton\(wrap, 'hint', '\+ Add hint'\)\)")

    def test_notes_by_managers_show_their_author_and_can_be_edited_by_them(self):
        i = self.item_id
        self.owner.post(f"/assignments/{self.aid}/hints/add", data={"target": f"i{i}", "text": "mine", "kind": "note", "time_minutes": "30"})
        page = self.owner.get(f"/assignments/{self.aid}").text
        m = re.search(r"var HINTS = (\{.*?\});\n", page, re.S)
        entries = json.loads(m.group(1))[f"i{i}"]
        self.assertEqual([(e["kind"], e["author"], e["time_minutes"], e["can_edit"]) for e in entries], [("note", "owner", 30, True)])
        note_id = entries[0]["id"]
        self.assertEqual(self.owner.post(f"/assignments/{self.aid}/hints/{note_id}/edit", data={"text": "mine, edited", "time_minutes": "45"}).status_code, 303)
        self.assertEqual(self.hints(), [("note", self.uid("owner"), 45, "mine, edited")])
        self.assertEqual(self.member.post(f"/assignments/{self.aid}/hints/{note_id}/edit", data={"text": "hijack"}).status_code, 403)

    def test_the_owner_can_delete_an_assignment_and_mark_progress_for_members(self):
        r = self.owner.post(f"/assignments/{self.aid}/items/{self.item_id}/solved", data={"user_id": self.uid("member"), "solved": "1"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.member.post(f"/assignments/{self.aid}/items/{self.item_id}/solved",
                                          data={"user_id": self.uid("owner"), "solved": "1"}).status_code, 403)
        self.assertEqual(self.owner.post(f"/assignments/{self.aid}/delete").status_code, 303)
        db = SessionLocal()
        self.assertEqual(db.query(models.Assignment).count(), 0)
        db.close()

    def test_leaving_a_group(self):
        r = self.member.post(f"/groups/{self.gid}/leave")
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/groups/"))
        self.assertEqual(self.group_row(self.gid)[2], ["owner"])
        self.assertEqual(self.member.get(f"/groups/{self.gid}").status_code, 403)     # and no more access
        r = self.owner.post(f"/groups/{self.gid}/leave")                              # the owner can't walk away
        self.assertEqual(r.headers["location"], f"/groups/{self.gid}")
        self.assertIn("you can't leave it", html.unescape(self.owner.get(f"/groups/{self.gid}").text))
        self.assertEqual(self.group_row(self.gid)[2], ["owner"])

    def test_admin_transfers_ownership(self):
        r = self.admin.post(f"/groups/{self.gid}/owner", data={"new_owner": "member"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.group_row(self.gid)[0], self.uid("member"))
        self.assertEqual(self.member.get(f"/groups/{self.gid}/edit-page").status_code, 200)   # the new owner manages it
        self.assertEqual(self.owner.get(f"/groups/{self.gid}/edit-page").status_code, 403)    # the old one no longer does
        # to a non-member, to a student, to nobody
        self.assertEqual(self.admin.post(f"/groups/{self.gid}/owner", data={"new_owner": "other"}).status_code, 422)
        self.assertEqual(self.admin.post(f"/groups/{self.gid}/owner", data={"new_owner": "ghost"}).status_code, 422)
        pupil = self.make_user("pupil", user_type="student", groups=[self.gid])
        self.assertEqual(self.admin.post(f"/groups/{self.gid}/owner", data={"new_owner": "pupil"}).status_code, 422)
        # and back to the admin
        self.assertEqual(self.admin.post(f"/groups/{self.gid}/owner", data={"new_owner": "admin"}).status_code, 303)
        self.assertEqual(self.group_row(self.gid)[0], 0)
        # only the admin may
        self.assertEqual(self.owner.post(f"/groups/{self.gid}/owner", data={"new_owner": "member"}).status_code, 403)

    def test_ownership_cannot_be_transferred_past_the_quota(self):
        for i in range(MAX_GROUPS_PER_USER):
            self.new_group(self.member, f"m{i}")
        r = self.admin.post(f"/groups/{self.gid}/owner", data={"new_owner": "member"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("already owns 5", r.text)

    def test_deleting_an_owner_returns_their_groups_to_the_admin(self):
        self.admin.post(f"/admin/users/{self.uid('owner')}/delete")
        row = self.group_row(self.gid)
        self.assertEqual((row[0], row[2]), (0, ["member"]))         # the group and its members survive
        self.assertEqual(self.admin.get(f"/groups/{self.gid}").status_code, 200)


class JoiningWithInvites(SiteTestCase):
    def setUp(self):
        super().setUp()
        self.owner = self.make_user("owner")
        self.gid, _ = self.new_group(self.owner, "Team", max_members=3)
        self.joiner = self.make_user("joiner")

    def test_opening_the_link_does_not_join_only_accepting_does(self):
        token = self.make_invite(self.owner, self.gid, max_uses=5)
        page = self.joiner.get(f"/invite/{token}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Join Team", page.text)
        self.assertIn("run by <strong>owner</strong>", re.sub(r"\s+", " ", page.text))
        self.assertEqual(self.group_row(self.gid)[2], ["owner"])    # a GET never adds anyone
        r = self.joiner.post(f"/invite/{token}/join")
        self.assertEqual((r.status_code, r.headers["location"]), (303, f"/groups/{self.gid}"))
        self.assertEqual(self.group_row(self.gid)[2], ["joiner", "owner"])
        db = SessionLocal()
        self.assertEqual(invites.find(db, token).uses, 1)
        db.close()

    def test_a_visitor_without_an_account_is_sent_to_sign_in(self):
        token = self.make_invite(self.owner, self.gid)
        page = TestClient(app).get(f"/invite/{token}")
        self.assertIn(f"/login?next=/invite/{token}", page.text)
        self.assertIn("Ask the administrator", page.text)            # no sign-up from an owner's link
        self.assertEqual(TestClient(app, follow_redirects=False).post(f"/invite/{token}/join").status_code, 303)  # -> login
        self.assertEqual(self.group_row(self.gid)[2], ["owner"])

    def test_login_then_back_to_the_link(self):
        token = self.make_invite(self.owner, self.gid)
        c = TestClient(app, follow_redirects=False)
        r = c.post("/login", data={"username": "joiner", "password": "secret-pass1", "next": f"/invite/{token}"})
        self.assertEqual(r.headers["location"], f"/invite/{token}")

    def test_refusals_leave_the_invite_and_the_group_alone(self):
        token = self.make_invite(self.owner, self.gid, max_uses=10)
        self.assertEqual(self.joiner.post(f"/invite/{token}/join").status_code, 303)
        again = self.joiner.post(f"/invite/{token}/join")            # already a member
        self.assertEqual(again.status_code, 409)
        self.assertIn("already in Team", again.text)
        db = SessionLocal()
        self.assertEqual(invites.find(db, token).uses, 1)             # the refused attempt didn't cost a use
        db.close()
        # the group has room for one more (limit 3): fill it, then the next person is refused
        self.assertEqual(self.make_user("third").post(f"/invite/{token}/join").status_code, 303)
        late = self.make_user("late").post(f"/invite/{token}/join")
        self.assertEqual(late.status_code, 409)
        self.assertIn("is full (3 members)", late.text)
        self.assertEqual(self.group_row(self.gid)[2], ["joiner", "owner", "third"])

    def test_dead_links_do_not_add_anyone(self):
        for how in ("revoke-by-owner", "expired", "used-up"):
            token = self.make_invite(self.owner, self.gid)
            db = SessionLocal()
            inv = invites.find(db, token)
            if how.startswith("revoke"):
                gid, iid = inv.group_id, inv.id
                db.close()
                self.assertEqual(self.owner.post(f"/groups/{gid}/invites/{iid}/revoke").status_code, 303)
            else:
                if how == "expired":
                    inv.expires_at = datetime.utcnow() - timedelta(hours=1)
                else:
                    inv.uses = inv.max_uses
                db.commit()
                db.close()
            r = self.joiner.post(f"/invite/{token}/join")
            self.assertEqual(r.status_code, 404, how)
            self.assertIn("no longer valid", r.text)
            self.assertIn("no longer valid", self.joiner.get(f"/invite/{token}").text)
        self.assertEqual(self.group_row(self.gid)[2], ["owner"])
        self.assertEqual(self.joiner.get("/invite/not-a-real-token").status_code, 404)

    def test_the_admin_cannot_join_as_a_member(self):
        token = self.make_invite(self.owner, self.gid)
        page = self.admin.get(f"/invite/{token}")
        self.assertIn("can't be a group member", html.unescape(page.text))
        self.assertEqual(self.admin.post(f"/invite/{token}/join").status_code, 409)

    def test_invites_of_one_group_cannot_be_revoked_through_another(self):
        other_owner = self.make_user("other")
        other_gid, _ = self.new_group(other_owner, "Elsewhere")
        token = self.make_invite(self.owner, self.gid)
        db = SessionLocal()
        iid = invites.find(db, token).id
        db.close()
        self.assertEqual(other_owner.post(f"/groups/{other_gid}/invites/{iid}/revoke").status_code, 303)  # no-op
        self.assertEqual(self.joiner.post(f"/invite/{token}/join").status_code, 303)                       # still valid
        self.assertEqual(other_owner.post(f"/groups/{self.gid}/invites/{iid}/revoke").status_code, 403)

    def test_the_link_is_shown_once_and_only_its_hash_is_stored(self):
        self.owner.post(f"/groups/{self.gid}/invites", data={"label": "cohort", "max_uses": 2})
        first = self.owner.get(f"/groups/{self.gid}").text
        m = re.search(r'value="https?://[^"]+/invite/([^"]+)" style', first)
        self.assertIsNotNone(m)
        token = m.group(1)
        second = self.owner.get(f"/groups/{self.gid}").text
        self.assertNotIn(token, second)                              # gone after the first view
        self.assertIn("cohort", second)                              # but the invite is listed, by label and hint
        self.assertIn(token[-4:], second)
        raw = sqlite3.connect(os.environ["DATABASE_URL"].replace("sqlite:///", "")).execute("SELECT * FROM invites").fetchall()
        self.assertFalse(any(token in str(row) for row in raw))
        # another member of the group never sees the flash either
        self.assertNotIn(token, self.joiner.get("/groups/").text)

    def test_owner_invites_respect_the_owners_limits_on_use_and_days(self):
        self.owner.post(f"/groups/{self.gid}/invites", data={"days": 9999, "max_uses": 9999})
        db = SessionLocal()
        inv = db.query(models.Invite).one()
        db.close()
        self.assertEqual(inv.max_uses, 100)
        self.assertLessEqual((inv.expires_at - datetime.utcnow()).days, 30)


class AdminInvitePage(SiteTestCase):
    def test_only_the_admin_reaches_it(self):
        member = self.make_user("someone")
        for method, url in (("get", "/admin/invites"), ("post", "/admin/invites/new"), ("post", "/admin/invites/1/revoke")):
            self.assertEqual(getattr(member, method)(url).status_code, 403, url)
        self.assertEqual(TestClient(app, follow_redirects=False).get("/admin/invites").status_code, 303)

    def test_create_list_and_revoke(self):
        token = self.make_platform_invite(label="autumn", max_uses=4, days=3, user_type="student")
        page = self.admin.get("/admin/invites").text
        self.assertNotIn(token, page)                                # shown once
        flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))
        for text in ("autumn", "0/4", "active", "New account (student)"):
            self.assertIn(text, flat)
        db = SessionLocal()
        iid = invites.find(db, token).id
        db.close()
        self.assertEqual(self.admin.post(f"/admin/invites/{iid}/revoke").status_code, 303)
        self.assertIn("revoked", self.admin.get("/admin/invites").text)
        self.assertEqual(TestClient(app, follow_redirects=False).get("/register", params={"invite": token}).status_code, 403)

    def test_owner_created_invites_appear_in_the_admin_list_with_their_group(self):
        owner = self.make_user("owner")
        gid, _ = self.new_group(owner, "Squad")
        self.make_invite(owner, gid, label="from the owner")
        flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", self.admin.get("/admin/invites").text))
        self.assertIn("from the owner", flat)
        self.assertIn("Squad", flat)


if __name__ == "__main__":
    unittest.main()
