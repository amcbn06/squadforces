"""The audit log (who did what, when) and the last-login tracking."""
import json
import re
import unittest
from datetime import datetime, timedelta
from unittest import mock

from fastapi.testclient import TestClient

from app import audit, models
from app.database import SessionLocal
from app.main import app
from app.templating import time_ago
from tests import test_invites_groups as base  # module import, so its test classes aren't collected twice
from tests.support import DbTestCase


class FakeRequest:
    def __init__(self, headers=None, host="10.0.0.1", session=None):
        self.headers = headers or {}
        self.client = mock.Mock(host=host) if host else None
        self.session = session or {}


# ── the service ────────────────────────────────────────────────────────────────────────────────

class AuditService(DbTestCase):
    def setUp(self):
        super().setUp()
        self.alice = self.user("alice")
        self.g, _ = self.group([self.alice])

    def rows(self):
        self.db.expire_all()
        return self.db.query(models.AuditEvent).order_by(models.AuditEvent.id).all()

    def test_an_event_snapshots_actor_target_and_details(self):
        req = FakeRequest({"user-agent": "TestBrowser/1.0", "x-forwarded-for": "203.0.113.7, 10.1.1.1"})
        audit.record(self.db, req, "group.member_add", actor=self.alice, target_type="user", target_id=9,
                     target_label="bob", group_id=self.g.id, details={"group": "G", "via": "form"}, commit=True)
        (e,) = self.rows()
        self.assertEqual((e.actor_id, e.actor_name, e.action, e.target_type, e.target_id, e.target_label, e.group_id),
                         (self.alice.id, "alice", "group.member_add", "user", 9, "bob", self.g.id))
        self.assertEqual(json.loads(e.details), {"group": "G", "via": "form"})
        self.assertEqual((e.ip, e.user_agent, e.ok), ("203.0.113.7", "TestBrowser/1.0", True))
        self.assertLess((datetime.utcnow() - e.at).total_seconds(), 5)

    def test_the_actor_defaults_to_whoever_the_session_is_signed_in_as(self):
        audit.record(self.db, FakeRequest(session={"user_id": self.alice.id}), "logout", commit=True)
        audit.record(self.db, FakeRequest(session={}), "login.failed", ok=False, commit=True)
        first, second = self.rows()
        self.assertEqual((first.actor_name, second.actor_name, second.ok), ("alice", None, False))

    def test_secrets_are_never_written(self):
        audit.record(self.db, FakeRequest(), "user.edit", actor=self.alice, commit=True, details={
            "password": "hunter2", "new_password": "hunter3", "invite_token": "abc", "password_hash": "x",
            "session_cookie": "y", "api_secret": "z", "changed": {"username": ["a", "b"]}, "password_reset": True})
        (e,) = self.rows()
        self.assertEqual(json.loads(e.details), {"changed": {"username": ["a", "b"]}})
        self.assertNotIn("hunter", e.details)

    def test_details_and_labels_are_bounded(self):
        audit.record(self.db, FakeRequest(), "user.edit", actor=self.alice, target_label="x" * 900,
                     details={"blob": "y" * 9000}, commit=True)
        (e,) = self.rows()
        self.assertLessEqual(len(e.target_label), 200)
        self.assertLessEqual(len(e.details), 2000)

    def test_the_event_commits_or_rolls_back_together_with_the_action(self):
        # an action that fails after the event was added leaves no event
        audit.record(self.db, FakeRequest(), "group.delete", actor=self.alice, target_label="G")
        self.g.name = "renamed"
        self.db.rollback()
        self.assertEqual(self.rows(), [])
        # and one that succeeds commits both in one go
        audit.record(self.db, FakeRequest(), "group.edit", actor=self.alice, target_label="G")
        self.g.name = "renamed"
        self.db.commit()
        self.assertEqual([e.action for e in self.rows()], ["group.edit"])
        self.assertEqual(self.db.get(models.Group, self.g.id).name, "renamed")

    def test_a_broken_audit_never_breaks_the_caller(self):
        broken = mock.Mock()
        broken.add.side_effect = RuntimeError("disk full")
        audit.record(broken, FakeRequest(), "logout", actor=self.alice, commit=True)   # must not raise
        broken.rollback.assert_called()

    def test_client_ip(self):
        self.assertEqual(audit.client_ip(FakeRequest({"x-forwarded-for": " 198.51.100.4 , 10.0.0.1"})), "198.51.100.4")
        self.assertEqual(audit.client_ip(FakeRequest({}, host="192.0.2.5")), "192.0.2.5")
        self.assertIsNone(audit.client_ip(FakeRequest({}, host=None)))
        self.assertIsNone(audit.client_ip(None))
        self.assertEqual(len(audit.client_ip(FakeRequest({"x-forwarded-for": "9" * 200}))), 45)

    def test_history_outlives_the_people_and_groups_it_mentions(self):
        audit.record(self.db, FakeRequest(), "group.create", actor=self.alice, target_type="group",
                     target_id=self.g.id, target_label="G", group_id=self.g.id, commit=True)
        self.db.delete(self.g)
        self.db.delete(self.alice)
        self.db.commit()
        (e,) = self.rows()
        self.assertEqual((e.actor_name, e.target_label), ("alice", "G"))

    def test_search_filters_order_and_paging(self):
        now = datetime.utcnow()
        specs = [("login.success", "alice", None, True, 1), ("login.failed", None, None, False, 2),
                 ("group.join", "bob", self.g.id, True, 3), ("group.leave", "bob", self.g.id, True, 40),
                 ("invite.create", "alice", self.g.id, True, 5)]
        for action, who, gid, ok, age_days in specs:
            self.db.add(models.AuditEvent(action=action, actor_name=who, group_id=gid, ok=ok, at=now - timedelta(days=age_days)))
        self.db.commit()

        def found(**kw):
            rows, total = audit.search(self.db, **kw)
            return [r.action for r in rows], total

        self.assertEqual(found()[0], ["login.success", "login.failed", "group.join", "invite.create", "group.leave"])  # newest first
        self.assertEqual(found(action="group.join"), (["group.join"], 1))
        self.assertEqual(found(action="group.")[1], 2)                       # a trailing dot means "all group events"
        self.assertEqual(found(action="group")[1], 0)                        # without it, an exact match
        self.assertEqual(found(actor="bob")[1], 2)
        self.assertEqual(found(group_id=self.g.id)[1], 3)
        self.assertEqual(found(days=10)[1], 4)                               # the 40-day-old one drops out
        self.assertEqual(found(only_failures=True), (["login.failed"], 1))
        rows, total = audit.search(self.db, limit=2, offset=2)
        self.assertEqual((len(rows), total), (2, 5))

    def test_a_groups_view_is_limited_to_its_own_group_actions(self):
        other, _ = self.group([self.alice], name="Other")
        for action, gid in [("group.join", self.g.id), ("invite.create", self.g.id), ("item.add", self.g.id),
                            ("login.success", None), ("user.edit", None), ("access.denied", None),
                            ("group.join", other.id)]:
            self.db.add(models.AuditEvent(action=action, group_id=gid))
        self.db.commit()
        seen = sorted(e.action for e in audit.for_group(self.db, self.g.id))
        self.assertEqual(seen, ["group.join", "invite.create", "item.add"])
        self.assertTrue(set(audit.GROUP_ACTIONS) <= set(audit.ACTION_LABELS))
        self.assertFalse(any(a.startswith(("login", "user", "access", "password")) for a in audit.GROUP_ACTIONS))

    def test_describe_reads_as_a_sentence(self):
        e = models.AuditEvent(action="group.owner_transfer", target_label="Team", details=json.dumps({"from": "a", "to": "b"}))
        self.assertEqual(audit.describe(e), "Transferred group ownership: Team (from=a, to=b)")
        self.assertEqual(audit.describe(models.AuditEvent(action="logout")), "Signed out")
        self.assertEqual(audit.describe(models.AuditEvent(action="something.new")), "something.new")

    def test_prune_removes_only_old_events(self):
        now = datetime.utcnow()
        self.db.add_all([models.AuditEvent(action="logout", at=now - timedelta(days=audit.RETENTION_DAYS + 1)),
                         models.AuditEvent(action="logout", at=now - timedelta(days=audit.RETENTION_DAYS - 1)),
                         models.AuditEvent(action="logout", at=now)])
        self.db.commit()
        self.assertEqual(audit.prune(self.db), 1)
        self.assertEqual(len(self.rows()), 2)

    def test_time_ago(self):
        now = datetime.utcnow()
        for delta, expected in [(timedelta(seconds=20), "just now"), (timedelta(minutes=1, seconds=5), "1 minute ago"),
                                (timedelta(hours=3, minutes=5), "3 hours ago"), (timedelta(days=1, hours=2), "1 day ago"),
                                (timedelta(days=75), "2 months ago"), (timedelta(days=800), "2 years ago")]:
            self.assertEqual(time_ago(now - delta), expected)
        self.assertEqual(time_ago(None), "")
        self.assertEqual(time_ago(now + timedelta(hours=1)), "just now")   # a clock a little ahead doesn't show a negative


# ── what the site records ─────────────────────────────────────────────────────────────────────

class AuditWeb(base.SiteTestCase):
    def events(self, **filters):
        db = SessionLocal()
        try:
            q = db.query(models.AuditEvent)
            for k, v in filters.items():
                q = q.filter(getattr(models.AuditEvent, k) == v)
            return q.order_by(models.AuditEvent.id).all()
        finally:
            db.close()

    def actions(self, **filters):
        return [e.action for e in self.events(**filters)]

    def last(self, action):
        found = self.events(action=action)
        self.assertTrue(found, f"no {action} event")
        return found[-1]

    def details(self, event):
        return json.loads(event.details) if event.details else {}

    # sign-in ----------------------------------------------------------------------------------

    def test_sign_in_failure_block_and_logout(self):
        self.make_user("ana")
        c = TestClient(app, follow_redirects=False)
        hdr = {"x-forwarded-for": "203.0.113.9", "user-agent": "Mozilla/5.0 test"}
        c.post("/login", data={"username": "ana", "password": "wrong-one"}, headers=hdr)
        c.post("/login", data={"username": "nobody-here", "password": "wrong-two"}, headers=hdr)
        r = c.post("/login", data={"username": "ana", "password": "secret-pass1"}, headers=hdr)
        self.assertEqual(r.status_code, 303)
        c.post("/logout", headers=hdr)
        for _ in range(5):
            c.post("/login", data={"username": "ghost", "password": "x"}, headers=hdr)
        c.post("/login", data={"username": "ghost", "password": "x"}, headers=hdr)                # now blocked

        fails = [e for e in self.events(action="login.failed") if e.target_label in ("ana", "nobody-here")]
        self.assertEqual([(e.target_label, self.details(e)["known_account"], e.actor_id, e.ok) for e in fails],
                         [("ana", True, None, False), ("nobody-here", False, None, False)])
        ok = self.last("login.success")
        self.assertEqual((ok.actor_name, ok.ip, ok.user_agent), ("ana", "203.0.113.9", "Mozilla/5.0 test"))
        self.assertEqual(self.last("logout").actor_name, "ana")                     # known even though the session is then cleared
        blocked = self.last("login.blocked")
        self.assertEqual((blocked.target_label, blocked.ok), ("ghost", False))
        self.assertGreater(self.details(blocked)["retry_in_seconds"], 0)

    def test_last_login_is_recorded_and_shown_to_the_admin_and_the_user_only(self):
        ana = self.make_user("ana")
        self.make_user("bo")
        db = SessionLocal()
        after_creation = db.query(models.User).filter_by(username="ana").one().last_login_at
        db.close()
        self.assertIsNotNone(after_creation)                                        # make_user signs in once
        before = datetime.utcnow()
        self.login("ana")
        db = SessionLocal()
        stamp = db.query(models.User).filter_by(username="ana").one().last_login_at
        never = db.query(models.User).filter_by(username="admin").one().last_login_at
        db.close()
        self.assertGreaterEqual(stamp, before - timedelta(seconds=2))
        self.assertIsNotNone(never)                                                 # the admin signed in in setUp

        users_page = re.sub(r"\s+", " ", self.admin.get("/admin/users").text)
        self.assertIn("<th>Last login</th>", users_page)
        self.assertIn("just now", users_page)
        self.assertIn("last sign-in just now", re.sub(r"\s+", " ", ana.get("/users/ana").text))       # their own
        self.assertIn("last sign-in", re.sub(r"\s+", " ", self.admin.get("/users/ana").text))          # the admin's
        other = self.login("bo")
        self.assertNotIn("last sign-in", other.get("/users/ana").text)                                # nobody else's
        self.assertNotIn("no sign-in recorded", other.get("/users/ana").text)

    def test_a_user_who_never_signed_in_shows_a_dash(self):
        db = SessionLocal()
        db.add(models.User(username="ghost", password_hash="x", user_type="user"))
        db.commit()
        db.close()
        page = re.sub(r"\s+", " ", self.admin.get("/admin/users").text)
        self.assertRegex(page, r'title="No sign-in recorded yet">—</td>')
        self.assertIn("no sign-in recorded yet", self.admin.get("/users/ghost").text)

    def test_password_change_is_recorded_without_the_passwords(self):
        ana = self.make_user("ana")
        r = ana.post("/account/password", data={"current_password": "secret-pass1", "new_password": "brand-new-pass9",
                                                 "confirm_password": "brand-new-pass9"})
        self.assertEqual(r.status_code, 303)
        e = self.last("password.change")
        self.assertEqual((e.actor_name, e.target_label), ("ana", "ana"))
        self.assertNotIn("brand-new", json.dumps([e.details, e.target_label]))

    # accounts ---------------------------------------------------------------------------------

    def test_registration_records_the_invite_used(self):
        token = self.make_platform_invite(label="autumn cohort", max_uses=2)
        c = TestClient(app, follow_redirects=False)
        c.post("/register", data={"username": "newbie", "password": "secret-pass1", "password2": "secret-pass1",
                                   "invite": token, "cf_handle": "", "atcoder_handle": "", "kilonova_handle": ""},
               headers={"x-forwarded-for": "198.51.100.20"})
        e = self.last("account.register")
        self.assertEqual((e.actor_name, e.target_label, e.ip), ("newbie", "newbie", "198.51.100.20"))
        self.assertEqual((self.details(e)["invite_label"], self.details(e)["account_type"]), ("autumn cohort", "user"))
        self.assertNotIn(token, json.dumps([e.details, e.target_label]))
        db = SessionLocal()
        self.assertIsNotNone(db.query(models.User).filter_by(username="newbie").one().last_login_at)
        db.close()

    def test_admin_user_changes_are_recorded_with_what_changed(self):
        gid, _ = self.new_group(self.admin, "Alpha")
        gid2, _ = self.new_group(self.admin, "Beta")
        self.make_user("ana", groups=[gid])
        create = self.last("user.create")
        self.assertEqual((create.actor_name, create.target_label, self.details(create)["groups"]), ("admin", "ana", ["Alpha"]))
        add = [e for e in self.events(action="group.member_add") if e.target_label == "ana"][0]
        self.assertEqual((add.group_id, add.actor_name, self.details(add)["via"]), (gid, "admin", "admin user form"))

        uid = self.uid("ana")
        self.admin.post(f"/admin/users/{uid}/edit", data={"username": "ana2", "password": "a-new-password-1", "user_type": "student",
                                                            "cf_handle": "", "atcoder_handle": "ana_ac", "group_ids": [gid2]})
        edit = self.last("user.edit")
        changed = self.details(edit)
        self.assertEqual(changed["username"], ["ana", "ana2"])
        self.assertEqual(changed["account_type"], ["user", "student"])
        self.assertEqual(changed["atcoder"], [None, "ana_ac"])
        self.assertEqual(changed["credentials"], "reset by admin")
        self.assertNotIn("a-new-password", edit.details)
        removed = self.events(action="group.member_remove")
        added = [e for e in self.events(action="group.member_add") if e.target_label == "ana2"]
        self.assertEqual([(e.group_id, self.details(e)["group"]) for e in removed], [(gid, "Alpha")])
        self.assertEqual([(e.group_id, self.details(e)["group"]) for e in added], [(gid2, "Beta")])

        self.admin.post(f"/admin/users/{uid}/delete")
        gone = self.last("user.delete")
        self.assertEqual((gone.target_label, gone.actor_name), ("ana2", "admin"))
        self.assertEqual(self.details(gone)["memberships"], 1)

    def test_a_no_op_edit_records_nothing(self):
        ana = self.make_user("ana")
        uid = self.uid("ana")
        n = len(self.events(action="user.edit"))
        self.admin.post(f"/admin/users/{uid}/edit", data={"username": "ana", "user_type": "user", "cf_handle": ""})
        self.assertEqual(len(self.events(action="user.edit")), n)

    def test_profile_edits_record_handle_changes(self):
        ana = self.make_user("ana")
        ana.post("/users/ana/edit", data={"full_name": "Ana P", "cf_handle": "", "atcoder_handle": "ana_ac", "kilonova_handle": ""})
        e = self.last("profile.edit")
        self.assertEqual(e.actor_name, "ana")
        self.assertEqual(self.details(e)["changed"], {"atcoder": [None, "ana_ac"], "full_name": [None, "Ana P"]})
        n = len(self.events(action="profile.edit"))
        ana.post("/users/ana/edit", data={"full_name": "Ana P", "cf_handle": "", "atcoder_handle": "ana_ac", "kilonova_handle": ""})
        self.assertEqual(len(self.events(action="profile.edit")), n)          # nothing changed, nothing recorded

    # groups -----------------------------------------------------------------------------------

    def test_group_lifecycle_is_recorded_with_the_right_actor_and_group(self):
        owner, member = self.make_user("owner"), self.make_user("member")
        gid, _ = self.new_group(owner, "Team", max_members=6)
        e = self.last("group.create")
        self.assertEqual((e.actor_name, e.group_id, e.target_label), ("owner", gid, "Team"))
        self.assertEqual(self.details(e)["max_members"], 6)

        owner.post(f"/groups/{gid}/edit", data={"name": "Team B", "max_members": 8})
        e = self.last("group.edit")
        self.assertEqual(self.details(e), {"max_members": [6, 8], "name": ["Team", "Team B"]})

        token = self.make_invite(owner, gid, label="cohort", max_uses=3)
        inv = self.last("invite.create")
        self.assertEqual((inv.actor_name, inv.group_id, inv.target_label), ("owner", gid, "cohort"))
        self.assertNotIn(token, json.dumps([inv.details, inv.target_label]))       # the link itself is never recorded

        member.post(f"/invite/{token}/join")
        joined = self.last("group.join")
        self.assertEqual((joined.actor_name, joined.group_id, self.details(joined)["invited_by"]), ("member", gid, "owner"))

        owner.post(f"/groups/{gid}/members/{self.uid('member')}/remove")
        removed = self.last("group.member_remove")
        self.assertEqual((removed.actor_name, removed.target_label, removed.group_id), ("owner", "member", gid))

        db = SessionLocal()
        iid = db.query(models.Invite).one().id
        db.close()
        owner.post(f"/groups/{gid}/invites/{iid}/revoke")
        self.assertEqual(self.last("invite.revoke").actor_name, "owner")

        self.admin.post(f"/groups/{gid}/members/add", data={"member_username": "member"})
        added = self.last("group.member_add")
        self.assertEqual((added.actor_name, added.target_label, self.details(added)["via"]), ("admin", "member", "add-member form"))
        member.post(f"/groups/{gid}/leave")
        left = self.last("group.leave")
        self.assertEqual((left.actor_name, left.group_id), ("member", gid))

        self.admin.post(f"/groups/{gid}/members/add", data={"member_username": "member"})
        self.admin.post(f"/groups/{gid}/owner", data={"new_owner": "member"})
        moved = self.last("group.owner_transfer")
        self.assertEqual((moved.actor_name, self.details(moved)), ("admin", {"from": "owner", "to": "member"}))

        self.admin.post(f"/groups/{gid}/delete")
        gone = self.last("group.delete")
        self.assertEqual((gone.actor_name, gone.target_label), ("admin", "Team B"))
        self.assertEqual(self.details(gone)["members"], 2)                        # owner and member at that moment

    def test_who_added_a_member_can_be_answered(self):
        """The reason the log exists: 'did the admin add him, or did he add himself?'"""
        owner = self.make_user("owner")
        gid, _ = self.new_group(owner, "Team")
        harmito = self.make_user("harmito")
        self.admin.post(f"/groups/{gid}/members/add", data={"member_username": "harmito"})
        adds = [e for e in self.events(action="group.member_add") if e.target_label == "harmito"]
        self.assertEqual([(e.actor_name, e.group_id) for e in adds], [("admin", gid)])
        # a member can't add anyone: the attempt is refused and recorded as such
        r = harmito.post(f"/groups/{gid}/members/add", data={"member_username": "owner"})
        self.assertEqual(r.status_code, 403)
        denied = self.last("access.denied")
        self.assertEqual((denied.actor_name, denied.ok, denied.target_label), ("harmito", False, f"/groups/{gid}/members/add"))
        self.assertEqual(self.details(denied)["method"], "POST")

    def test_assignments_and_items_are_recorded(self):
        owner = self.make_user("owner")
        gid, _ = self.new_group(owner, "Team")
        r = owner.post("/assignments/new", data={"group_id": gid, "title": "Week 1"})
        aid = int(re.search(r"/assignments/(\d+)", r.headers["location"]).group(1))
        self.assertEqual(self.last("assignment.create").group_id, gid)
        owner.post(f"/assignments/{aid}/items/add", data={"item_type": "problem", "platform": "cses", "external_id": "1068"})
        e = self.last("item.add")
        self.assertEqual((e.actor_name, e.group_id, self.details(e)["platform"]), ("owner", gid, "cses"))
        owner.post(f"/assignments/{aid}/items/bulk", data={"links": "https://cses.fi/problemset/task/1083 https://cses.fi/problemset/task/1094"})
        self.assertEqual(self.details(self.last("item.add"))["count"], 2)
        db = SessionLocal()
        item_id = db.query(models.AssignmentItem).first().id
        db.close()
        owner.post(f"/assignments/{aid}/items/{item_id}/delete")
        self.assertEqual(self.last("item.delete").actor_name, "owner")
        owner.post(f"/assignments/{aid}/delete")
        gone = self.last("assignment.delete")
        self.assertEqual((gone.target_label, gone.group_id), ("Week 1", gid))

    def test_refusals_are_recorded_but_signed_out_noise_is_not(self):
        member = self.make_user("member")
        self.assertEqual(member.get("/admin/audit").status_code, 403)
        denied = self.last("access.denied")
        self.assertEqual((denied.actor_name, denied.target_label, denied.ok), ("member", "/admin/audit", False))
        before = len(self.events())
        TestClient(app, follow_redirects=False).get("/admin/audit")                # not signed in: just a redirect to login
        self.assertEqual(len(self.events()), before)

    def test_no_secret_reaches_the_log(self):
        secrets = ["secret-pass1", "brand-new-pass9", "a-new-password-1"]
        token = self.make_platform_invite(label="x")
        c = TestClient(app, follow_redirects=False)
        c.post("/register", data={"username": "newbie", "password": secrets[0], "password2": secrets[0], "invite": token,
                                   "cf_handle": "", "atcoder_handle": "", "kilonova_handle": ""})
        ana = self.make_user("ana")
        ana.post("/account/password", data={"current_password": secrets[0], "new_password": secrets[1], "confirm_password": secrets[1]})
        self.admin.post(f"/admin/users/{self.uid('ana')}/edit", data={"username": "ana", "password": secrets[2], "user_type": "user", "cf_handle": ""})
        TestClient(app, follow_redirects=False).post("/login", data={"username": "ana", "password": "guess-1234"})
        owner = self.make_user("owner")
        gid, _ = self.new_group(owner, "Team")
        gtoken = self.make_invite(owner, gid)
        blob = json.dumps([[getattr(e, c.name) for c in models.AuditEvent.__table__.columns] for e in self.events()], default=str)
        for secret in secrets + ["guess-1234", token, gtoken]:
            self.assertNotIn(secret, blob, secret)
        self.assertNotIn(invites_hash(token), blob)

    # the pages ---------------------------------------------------------------------------------

    def test_the_admin_page_filters_and_escapes(self):
        owner = self.make_user("owner")
        gid, _ = self.new_group(owner, "Team")
        TestClient(app, follow_redirects=False).post("/login", data={"username": "<script>alert(1)</script>", "password": "x"},
                                                     headers={"x-forwarded-for": "203.0.113.50"})
        page = self.admin.get("/admin/audit").text
        self.assertNotIn("<script>alert(1)</script>", page)                        # attacker-chosen text is escaped
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("203.0.113.50", page)                                        # the admin sees IPs
        flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))
        self.assertIn("Created a group: Team", flat)
        only_groups = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", self.admin.get("/admin/audit", params={"action": "group."}).text))
        self.assertIn("Created a group", only_groups)
        self.assertNotIn("Failed sign-in", only_groups.split("What")[-1].split("events")[-1])
        fails = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", self.admin.get("/admin/audit", params={"failures": "1"}).text))
        self.assertIn("Failed sign-in", fails)
        self.assertNotIn("Created a group: Team", fails)
        by_actor = self.admin.get("/admin/audit", params={"actor": "owner"}).text
        self.assertIn("owner", by_actor)
        self.assertNotIn("203.0.113.50", by_actor)

    def test_the_admin_page_pages_through_a_long_log(self):
        db = SessionLocal()
        now = datetime.utcnow()
        db.add_all([models.AuditEvent(action="logout", actor_name=f"u{i}", at=now - timedelta(seconds=i)) for i in range(250)])
        db.commit()
        db.close()
        first = self.admin.get("/admin/audit").text
        self.assertIn("page 1 of 3", first)
        self.assertIn("u0", first)
        self.assertNotIn(">u249<", first)
        last = self.admin.get("/admin/audit", params={"page": 3}).text
        self.assertIn("u249", last)
        self.assertNotIn("Older", last)
        self.assertEqual(self.admin.get("/admin/audit", params={"page": 999}).status_code, 200)   # past the end: empty, no crash
        self.assertEqual(self.admin.get("/admin/audit", params={"page": -4, "days": "x"}).status_code, 422)  # junk is rejected, not trusted

    def test_only_the_admin_reads_the_log(self):
        member = self.make_user("member")
        self.assertEqual(member.get("/admin/audit").status_code, 403)
        self.assertEqual(TestClient(app, follow_redirects=False).get("/admin/audit").status_code, 303)
        self.assertIn("Audit log", self.admin.get("/admin/users").text)               # linked from the users page

    def test_an_owner_sees_their_groups_changes_without_ips_or_other_groups(self):
        owner, member = self.make_user("owner"), self.make_user("member")
        gid, _ = self.new_group(owner, "Team")
        other = self.make_user("other")
        other_gid, _ = self.new_group(other, "Elsewhere")
        token = self.make_invite(owner, gid, max_uses=2)
        member.post(f"/invite/{token}/join", headers={"x-forwarded-for": "203.0.113.77"})
        page = owner.get(f"/groups/{gid}").text
        flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))
        self.assertIn("Recent changes", flat)
        self.assertIn("Joined a group with an invite link: Team", flat)
        self.assertIn("Created an invite link", flat)
        self.assertNotIn("203.0.113.77", page)                                      # no IPs for owners
        self.assertNotIn("Signed in", flat)                                         # nothing account-level
        self.assertNotIn("Elsewhere", flat)                                         # nothing from other groups
        self.assertNotIn("Recent changes", member.get(f"/groups/{gid}").text)      # members don't get the panel


def invites_hash(token):
    from app import invites
    return invites.hash_token(token)


class Migration(DbTestCase):
    def test_last_login_column_is_added_to_an_old_users_table(self):
        import os
        import sqlite3
        import subprocess
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db").replace("\\", "/")
            con = sqlite3.connect(path)
            con.executescript("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(50), password_hash VARCHAR(200));"
                              "INSERT INTO users VALUES (0, 'admin', 'x'), (1, 'a', 'y');")
            con.commit()
            con.close()
            code = ("from app.database import ensure_columns, engine\nfrom sqlalchemy import text\n"
                    "ensure_columns(); ensure_columns()\n"
                    "with engine.connect() as c: print(c.execute(text('SELECT id, last_login_at FROM users ORDER BY id')).all())\n")
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                 env={**os.environ, "DATABASE_URL": "sqlite:///" + path}, cwd=os.getcwd())
            self.assertEqual(out.returncode, 0, out.stderr[-400:])
            self.assertEqual(out.stdout.strip(), "[(0, None), (1, None)]")


if __name__ == "__main__":
    unittest.main()
