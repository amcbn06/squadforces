"""A group's "Stars" switch: the owner stars problems, members see only the starred ones."""
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from fastapi.testclient import TestClient

from app import models
from app.database import SessionLocal
from app.main import app
from tests.test_invites_groups import SiteTestCase


class Stars(SiteTestCase):
    def setUp(self):
        super().setUp()
        self.owner = self.make_user("coach")
        self.gid, _ = self.new_group(self.owner, "Team", stars=True)
        self.member = self.make_user("alice", groups=[self.gid])
        self.outsider = self.make_user("eve")
        self.aid = self.new_assignment(self.owner, self.gid)
        self.owner.post(f"/assignments/{self.aid}/items/add",
                        data={"item_type": "problem", "platform": "cses", "external_id": "1068"})
        db = SessionLocal()
        self.item = db.query(models.AssignmentItem).one().id
        contest = models.AssignmentItem(assignment_id=self.aid, type="contest", platform="codeforces",
                                        external_id="2085", title="Round", sync_status="done")
        db.add(contest)
        db.flush()
        self.contest = contest.id
        for index in ("A", "B"):
            db.add(models.ContestProblem(assignment_item_id=contest.id, platform_problem_id=f"2085{index}",
                                         index=index, name=f"Problem {index}"))
        db.commit()
        self.cp = [c.id for c in db.query(models.ContestProblem).order_by(models.ContestProblem.index)]
        db.close()

    def new_assignment(self, client, gid, title="Week 1"):
        r = client.post("/assignments/new", data={"group_id": gid, "title": title})
        return int(re.search(r"/assignments/(\d+)", r.headers["location"]).group(1))

    def star(self, client, target, on=True, aid=None):
        return client.post(f"/assignments/{aid or self.aid}/stars", data={"target": target, "starred": "1" if on else "0"})

    def page(self, client, aid=None):
        return client.get(f"/assignments/{aid or self.aid}").text

    def starred(self):
        db = SessionLocal()
        try:
            return (sorted(i.id for i in db.query(models.AssignmentItem).filter_by(starred=True)),
                    sorted(c.id for c in db.query(models.ContestProblem).filter_by(starred=True)))
        finally:
            db.close()

    # what each person sees ------------------------------------------------------------------------------------

    def test_the_owner_sees_a_grey_star_on_every_problem_and_a_member_sees_none(self):
        owner = self.page(self.owner)
        self.assertEqual(owner.count('<button type="button" class="star-btn"'), 3)   # one standalone, two in the contest
        self.assertNotIn("star-btn on", owner)
        for key in (f"i{self.item}", f"p{self.cp[0]}", f"p{self.cp[1]}"):
            self.assertIn(f'data-key="{key}"', owner)
        self.assertNotIn("star-btn", self.page(self.member))   # nothing starred: nothing to show

    def test_a_starred_problem_is_yellow_for_the_owner_and_shown_read_only_to_members(self):
        self.assertEqual(self.star(self.owner, f"i{self.item}").status_code, 200)
        self.assertEqual(self.star(self.owner, f"p{self.cp[1]}").status_code, 200)
        self.assertEqual(self.starred(), ([self.item], [self.cp[1]]))
        owner = self.page(self.owner)
        self.assertEqual(owner.count('class="star-btn on"'), 2)
        self.assertEqual(owner.count('<button type="button" class="star-btn"'), 1)  # the one left unstarred stays grey
        self.assertIn('aria-pressed="true"', owner)
        member = self.page(self.member)
        self.assertEqual(member.count('class="star-btn on" role="img"'), 2)         # starred ones only, not clickable
        self.assertNotIn("<button", member.split('class="star-btn on"')[1].split("</span>")[0])
        self.assertNotIn('<button type="button" class="star-btn', member)
        self.assertNotIn("toggleStar", member)

    def test_the_admin_can_star_in_any_group(self):
        self.assertEqual(self.star(self.admin, f"i{self.item}").status_code, 200)
        self.assertEqual(self.starred()[0], [self.item])

    def test_the_response_says_where_the_star_ended_up(self):
        self.assertEqual(self.star(self.owner, f"i{self.item}").json(), {"starred": True})
        self.assertEqual(self.star(self.owner, f"i{self.item}", on=False).json(), {"starred": False})

    # setting it ---------------------------------------------------------------------------------------------------

    def test_asking_for_the_same_state_twice_changes_and_records_nothing_the_second_time(self):
        self.star(self.owner, f"i{self.item}")
        self.star(self.owner, f"i{self.item}")
        self.star(self.owner, f"i{self.item}", on=False)
        self.star(self.owner, f"i{self.item}", on=False)
        db = SessionLocal()
        actions = [e.action for e in db.query(models.AuditEvent).filter(models.AuditEvent.action.like("assignment.%star"))
                   .order_by(models.AuditEvent.id)]
        db.close()
        self.assertEqual(actions, ["assignment.star", "assignment.unstar"])
        self.assertEqual(self.starred(), ([], []))

    def test_starring_shows_in_the_groups_recent_changes(self):
        self.star(self.owner, f"p{self.cp[0]}")
        group_page = self.owner.get(f"/groups/{self.gid}").text
        self.assertIn("Starred a problem on an assignment", group_page)

    def test_only_the_owner_or_admin_may_star(self):
        self.assertEqual(self.star(self.member, f"i{self.item}").status_code, 403)
        self.assertEqual(self.star(self.outsider, f"i{self.item}").status_code, 403)
        anonymous = TestClient(app, follow_redirects=False)
        self.assertIn(self.star(anonymous, f"i{self.item}").status_code, (303, 401))
        self.assertEqual(self.starred(), ([], []))

    def test_a_bad_target_is_refused(self):
        for target, status in [("", 400), ("x1", 400), ("i", 400), ("iabc", 400), ("i99999", 404), ("p99999", 404),
                               (f"i{self.contest}", 404)]:   # a contest as a whole can't be starred, only its problems
            self.assertEqual(self.star(self.owner, target).status_code, status, target)
        self.assertEqual(self.star(self.owner, "i1", aid=99999).status_code, 404)

    def test_a_problem_of_another_assignment_cannot_be_starred_through_this_one(self):
        other = self.new_assignment(self.owner, self.gid, "Week 2")
        self.assertEqual(self.star(self.owner, f"i{self.item}", aid=other).status_code, 404)
        self.assertEqual(self.star(self.owner, f"p{self.cp[0]}", aid=other).status_code, 404)
        self.assertEqual(self.starred(), ([], []))

    # the switch ---------------------------------------------------------------------------------------------------

    def test_switched_off_means_no_stars_and_no_starring_but_nothing_is_forgotten(self):
        self.star(self.owner, f"i{self.item}")
        self.owner.post(f"/groups/{self.gid}/edit", data={"name": "Team", "max_members": 10})   # stars unticked
        for client in (self.owner, self.member):
            page = self.page(client)
            self.assertNotIn("star-btn", page)
            self.assertNotIn("toggleStar", page)
        r = self.star(self.owner, f"p{self.cp[0]}")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.starred(), ([self.item], []))               # the earlier star is still stored
        self.owner.post(f"/groups/{self.gid}/edit", data={"name": "Team", "max_members": 10, "stars_allowed": "1"})
        self.assertIn('class="star-btn on" role="img"', self.page(self.member))   # and shows again

    def test_a_group_made_without_the_switch_has_no_stars(self):
        gid, _ = self.new_group(self.owner, "Plain")
        aid = self.new_assignment(self.owner, gid)
        self.owner.post(f"/assignments/{aid}/items/add", data={"item_type": "problem", "platform": "cses", "external_id": "1083"})
        self.assertNotIn("star-btn", self.page(self.owner, aid))
        db = SessionLocal()
        item = db.query(models.AssignmentItem).filter_by(assignment_id=aid).one().id
        db.close()
        self.assertEqual(self.star(self.owner, f"i{item}", aid=aid).status_code, 400)

    def test_the_group_form_has_the_switch_and_remembers_it(self):
        self.assertIn('name="stars_allowed"', self.owner.get("/groups/new").text)
        edit = self.owner.get(f"/groups/{self.gid}/edit-page").text
        self.assertRegex(edit, r'name="stars_allowed" value="1"\s*checked')
        self.owner.post(f"/groups/{self.gid}/edit", data={"name": "Team", "max_members": 10})
        self.assertNotRegex(self.owner.get(f"/groups/{self.gid}/edit-page").text, r'name="stars_allowed" value="1"\s*checked')
        self.assertIn("Edited a group", self.owner.get(f"/groups/{self.gid}").text)   # the change is in the audit trail

    def test_the_toggle_script_is_only_for_those_who_can_use_it(self):
        self.assertIn("function toggleStar", self.page(self.owner))
        self.assertNotIn("function toggleStar", self.page(self.member))


class UpgradingAnOldDatabase(unittest.TestCase):
    def test_the_new_columns_are_added_and_existing_rows_start_unstarred(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db").replace("\\", "/")
            con = sqlite3.connect(path)
            con.executescript("""
                CREATE TABLE groups (id INTEGER PRIMARY KEY, name VARCHAR(100), hints_allowed BOOLEAN NOT NULL DEFAULT 0);
                CREATE TABLE assignment_items (id INTEGER PRIMARY KEY, assignment_id INTEGER, type VARCHAR(10), platform VARCHAR(15));
                CREATE TABLE contest_problems (id INTEGER PRIMARY KEY, assignment_item_id INTEGER, name VARCHAR(300));
                INSERT INTO groups (id, name) VALUES (1, 'Old');
                INSERT INTO assignment_items VALUES (1, 1, 'problem', 'cses');
                INSERT INTO contest_problems VALUES (1, 1, 'A');
            """)
            con.commit()
            con.close()
            code = ("from app.database import ensure_columns, engine\n"
                    "from sqlalchemy import text\n"
                    "ensure_columns(); ensure_columns()\n"
                    "with engine.connect() as c:\n"
                    "    for q in ('groups.stars_allowed', 'assignment_items.starred', 'contest_problems.starred'):\n"
                    "        t, col = q.split('.')\n"
                    "        print(q, c.execute(text(f'SELECT {col} FROM {t}')).scalar())\n")
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                 env={**os.environ, "DATABASE_URL": "sqlite:///" + path}, cwd=os.getcwd())
            self.assertEqual(out.returncode, 0, out.stderr[-500:])
            self.assertEqual(out.stdout.split(), ["groups.stars_allowed", "0", "assignment_items.starred", "0",
                                                  "contest_problems.starred", "0"])


if __name__ == "__main__":
    unittest.main()
