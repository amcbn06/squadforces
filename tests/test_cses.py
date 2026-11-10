"""Counting CSES solves: the member's browser sends the list, the app records it as submissions and checks it against
the one public number CSES shows (the account's total submissions). The network is faked; nothing here calls cses.fi."""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from fastapi.testclient import TestClient

from app import cses_import, leaderboard, models, submissions
from app.cses_import import ImportRejected, PublicProfile
from app.database import SessionLocal
from app.main import app
from app.sync import sync_item
from tests.support import DbTestCase
from tests.test_invites_groups import SiteTestCase

NOW = 1_800_000_000


def profile(count, name="Ana"):
    return mock.patch("app.cses_import.fetch_public_profile", new=mock.AsyncMock(return_value=PublicProfile(name, count)))


PUBLIC_PAGE = """<div class="title-block"><h1>User pllk</h1></div>
<h2>User information</h2><table class="summary-table narrow">
<tr><td >Submission count:</td><td >6524</td></tr>
<tr><td >First submission:</td><td >2015-01-08 12:14:35</td></tr></table>"""


class PublicProfileParsing(unittest.TestCase):
    def test_name_and_count_come_from_the_public_page(self):
        self.assertEqual(cses_import.parse_profile(PUBLIC_PAGE), PublicProfile("pllk", 6524))
        self.assertEqual(cses_import.parse_profile(PUBLIC_PAGE.replace("pllk", "A &amp; B")).name, "A & B")

    def test_anything_else_is_not_a_profile(self):
        self.assertIsNone(cses_import.parse_profile("<h1>404 Not Found</h1>"))
        self.assertIsNone(cses_import.parse_profile(""))

    def test_a_missing_account_is_none_and_an_unreadable_site_is_a_refusal_not_a_pass(self):
        import httpx

        class Reply:
            def __init__(self, status, text=""):
                self.status_code, self.text = status, text

        async def run(reply):
            with mock.patch("httpx.AsyncClient.get", new=mock.AsyncMock(side_effect=reply if isinstance(reply, Exception) else None,
                                                                         return_value=None if isinstance(reply, Exception) else reply)):
                return await cses_import.fetch_public_profile("1")

        import asyncio
        self.assertIsNone(asyncio.run(run(Reply(404))))
        self.assertEqual(asyncio.run(run(Reply(200, PUBLIC_PAGE))), PublicProfile("pllk", 6524))
        for broken in (Reply(500), Reply(200, "<html>changed</html>"), httpx.ConnectTimeout("slow")):
            with self.assertRaises(ImportRejected) as raised:
                asyncio.run(run(broken))
            self.assertEqual(raised.exception.status, 503)  # never accepted without the check


class Importing(DbTestCase):
    def setUp(self):
        super().setUp()
        self.ana = self.user("ana")
        self.bob = self.user("bob")
        self.group_, self.assignment = self.group([self.ana, self.bob])

    async def run_import(self, user, cses_id, solved, count, *, now=NOW, name="Ana"):
        with profile(count, name):
            result = await cses_import.import_solved(self.db, user, cses_id, solved, now=now)
        self.db.commit()
        return result

    def rows(self, user):
        return self.db.query(models.Submission).filter_by(user_id=user.id, platform="cses").order_by(models.Submission.problem_key).all()

    def state(self, user):
        return self.db.query(models.SubmissionSync).filter_by(user_id=user.id, platform="cses").one()

    # the first import -------------------------------------------------------------------------------------------

    async def test_the_first_import_records_the_history_without_counting_it_as_new(self):
        result = await self.run_import(self.ana, "4242", ["1068", "1083", "1069"], count=40)
        self.assertEqual((result.solved, result.new, result.baseline), (3, 3, True))
        self.assertEqual([r.problem_key for r in self.rows(self.ana)], ["1068", "1069", "1083"])
        self.assertTrue(all(r.backfilled and r.accepted for r in self.rows(self.ana)))
        state = self.state(self.ana)
        self.assertEqual((state.handle, state.submission_count), ("4242", 40))
        self.assertIsNotNone(state.full_sync_at)  # without it the leaderboard would ignore the user entirely
        self.assertEqual(self.db.get(models.User, self.ana.id).cses_id, "4242")
        # old solves are not "this month's": nothing time-based sees them
        self.assertEqual(leaderboard.new_solves(self.db, [self.ana.id], since=0), {})
        self.assertEqual(submissions.recent(self.db, self.ana.id), [])
        self.assertIsNone(submissions.earliest_submission_at(self.db, self.ana.id, "cses"))
        self.assertEqual(submissions.daily_solved_counts(self.db, self.ana.id, "cses", 0), {})

    async def test_it_marks_the_matching_items_of_her_groups_solved_and_nothing_else(self):
        mine = self.item(self.assignment, "cses", "problem", "1068")
        unsolved = self.item(self.assignment, "cses", "problem", "1090")
        other_judge = self.item(self.assignment, "kilonova", "problem", "1068")
        _, elsewhere = self.group([self.bob], name="Other")      # she is not in that group
        not_hers = self.item(elsewhere, "cses", "problem", "1068")
        await self.run_import(self.ana, "4242", ["1068"], count=10)
        solved = {r.assignment_item_id for r in self.db.query(models.Result).filter_by(user_id=self.ana.id, solved=True)}
        self.assertEqual(solved, {mine.id})
        self.assertIsNone(self.db.query(models.Result).filter_by(assignment_item_id=mine.id).one().solve_time)  # unknown time
        self.assertEqual(self.db.query(models.Result).filter(models.Result.assignment_item_id.in_(
            [unsolved.id, other_judge.id, not_hers.id])).count(), 0)

    async def test_for_a_member_who_imported_cses_is_the_truth_so_a_stale_hand_mark_goes(self):
        item = self.item(self.assignment, "cses", "problem", "1090")
        self.db.add_all([models.Result(assignment_item_id=item.id, user_id=self.ana.id, solved=True),
                         models.Result(assignment_item_id=item.id, user_id=self.bob.id, solved=True)])
        self.db.commit()
        await self.run_import(self.ana, "4242", ["1068"], count=10)   # her list doesn't include 1090
        marked = {r.user_id for r in self.db.query(models.Result).filter_by(assignment_item_id=item.id)}
        self.assertEqual(marked, {self.bob.id})                      # hers is gone; bob never imported, so his stays

    async def test_a_later_import_clears_a_mark_the_import_no_longer_has_and_a_sync_agrees(self):
        item = self.item(self.assignment, "cses", "problem", "1068")
        await self.run_import(self.ana, "4242", ["1068"], count=10, now=NOW)
        self.assertEqual(self.db.query(models.Result).filter_by(assignment_item_id=item.id, user_id=self.ana.id).count(), 1)
        self.db.add(models.Result(assignment_item_id=item.id, user_id=self.bob.id, solved=True))   # bob ticks by hand
        self.db.commit()
        await sync_item(item.id, self.db)                             # a refresh never touches an unlinked member
        self.assertEqual(self.db.query(models.Result).filter_by(assignment_item_id=item.id).count(), 2)

    # what the matrix cell does ------------------------------------------------------------------------------------

    def test_the_cell_is_locked_only_for_a_member_who_imported(self):
        from app.platforms import registry
        cses, pbinfo = registry.get("cses"), registry.get("pbinfo")
        item = self.item(self.assignment, "cses", "problem", "1068")
        linked = models.User(username="linked", password_hash="x", cses_id="77")
        self.assertIsNone(cses.locked_status(item, self.ana, True))             # no import: a mark by hand, clickable
        self.assertEqual(cses.locked_status(item, linked, True), "Solved on CSES (from the last import)")
        self.assertIn("Import again", cses.locked_status(item, linked, False))
        self.assertIn("rankings", cses.manual_hint(item))
        self.assertIsNone(pbinfo.locked_status(item, linked, True))             # other manual platforms are untouched
        self.assertEqual(pbinfo.manual_hint(item), "")

    # later imports --------------------------------------------------------------------------------------------------

    async def test_solves_that_appear_later_count_from_the_import(self):
        item = self.item(self.assignment, "cses", "problem", "1090")
        await self.run_import(self.ana, "4242", ["1068"], count=10, now=NOW)
        result = await self.run_import(self.ana, "4242", ["1068", "1090", "1091"], count=13, now=NOW + 3600)
        self.assertEqual((result.solved, result.new, result.baseline), (3, 2, False))
        fresh = [r for r in self.rows(self.ana) if not r.backfilled]
        self.assertEqual([(r.problem_key, r.submitted_at) for r in fresh], [("1090", NOW + 3600), ("1091", NOW + 3600)])
        self.assertEqual(leaderboard.new_solves(self.db, [self.ana.id], since=NOW), {self.ana.id: {("cses", "1090"), ("cses", "1091")}})
        # and the group board counts only the one that is in the group's assignments
        self.db.expire_all()
        board = {row.user.username: row.solved for row in leaderboard.for_group(self.db, self.group_)}
        self.assertEqual(board["ana"], 0 if leaderboard.window_start() > NOW + 3600 else 1)
        result_row = self.db.query(models.Result).filter_by(assignment_item_id=item.id, user_id=self.ana.id).one()
        self.assertTrue(result_row.solved)
        self.assertEqual(result_row.solve_time, datetime.utcfromtimestamp(NOW + 3600))

    async def test_importing_the_same_list_again_changes_nothing(self):
        await self.run_import(self.ana, "4242", ["1068", "1083"], count=10, now=NOW)
        result = await self.run_import(self.ana, "4242", ["1068", "1083"], count=10, now=NOW + 3600)
        self.assertEqual((result.new, result.baseline), (0, False))
        self.assertEqual(len(self.rows(self.ana)), 2)

    async def test_a_task_new_in_the_list_is_the_only_thing_added(self):
        await self.run_import(self.ana, "4242", ["1068"], count=10, now=NOW)
        await self.run_import(self.ana, "4242", ["1068", "1083"], count=11, now=NOW + 3600)
        self.assertEqual([(r.problem_key, r.backfilled) for r in self.rows(self.ana)], [("1068", True), ("1083", False)])

    # the checks -------------------------------------------------------------------------------------------------

    async def test_nobody_has_more_solved_tasks_than_submissions(self):
        with self.assertRaises(ImportRejected) as raised:
            await self.run_import(self.ana, "4242", ["1", "2", "3"], count=2)
        self.assertIn("only 2 submissions", raised.exception.message)
        self.assertEqual(self.rows(self.ana), [])

    async def test_new_solves_need_new_submissions_on_the_public_account(self):
        await self.run_import(self.ana, "4242", ["1068"], count=10, now=NOW)
        with self.assertRaises(ImportRejected) as raised:                  # 3 new solves, but only 1 new submission
            await self.run_import(self.ana, "4242", ["1068", "1", "2", "3"], count=11, now=NOW + 3600)
        self.assertIn("1 new submission", raised.exception.message)
        self.assertEqual([r.problem_key for r in self.rows(self.ana)], ["1068"])
        self.assertEqual(self.state(self.ana).submission_count, 10)          # the failed attempt moved nothing
        await self.run_import(self.ana, "4242", ["1068", "1", "2", "3"], count=13, now=NOW + 7200)  # 3 new: fine

    async def test_one_cses_account_belongs_to_one_member(self):
        await self.run_import(self.ana, "4242", ["1068"], count=10)
        with self.assertRaises(ImportRejected) as raised:
            await self.run_import(self.bob, "4242", ["1068"], count=10)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.rows(self.bob), [])
        self.assertIsNone(self.db.get(models.User, self.bob.id).cses_id)

    async def test_there_must_be_such_a_cses_user(self):
        with mock.patch("app.cses_import.fetch_public_profile", new=mock.AsyncMock(return_value=None)):
            with self.assertRaises(ImportRejected) as raised:
                await cses_import.import_solved(self.db, self.ana, "999999", ["1068"], now=NOW)
        self.assertEqual(raised.exception.status, 404)

    async def test_a_second_import_within_a_minute_is_refused(self):
        await self.run_import(self.ana, "4242", ["1068"], count=10, now=NOW)
        with self.assertRaises(ImportRejected) as raised:
            await self.run_import(self.ana, "4242", ["1068"], count=10, now=NOW + 30)
        self.assertEqual(raised.exception.status, 429)
        await self.run_import(self.ana, "4242", ["1068"], count=10, now=NOW + 61)

    async def test_a_refused_import_still_uses_up_the_cooldown(self):
        with self.assertRaises(ImportRejected):
            await self.run_import(self.ana, "4242", ["1", "2", "3"], count=1, now=NOW)
        self.db.commit()  # what the route does
        with self.assertRaises(ImportRejected) as raised:
            await self.run_import(self.ana, "4242", ["1"], count=5, now=NOW + 5)
        self.assertEqual(raised.exception.status, 429)

    async def test_bad_input_is_refused_before_cses_is_asked(self):
        async def attempt(cses_id, solved):
            with profile(100) as fetched:
                with self.assertRaises(ImportRejected):
                    await cses_import.import_solved(self.db, self.ana, cses_id, solved, now=NOW)
                self.assertFalse(fetched.called)

        await attempt("", ["1068"])
        await attempt("abc", ["1068"])
        await attempt("1234567890", ["1068"])
        await attempt("4242", "1068")
        await attempt("4242", ["10 68"])
        await attempt("4242", ["<b>"])
        await attempt("4242", [str(i) for i in range(cses_import.MAX_SOLVED + 1)])

    # changing account --------------------------------------------------------------------------------------------

    async def test_a_different_cses_account_starts_over(self):
        await self.run_import(self.ana, "4242", ["1068", "1083"], count=10, now=NOW)
        await self.run_import(self.ana, "5555", ["1069"], count=4, now=NOW + 3600)
        self.assertEqual([(r.problem_key, r.backfilled) for r in self.rows(self.ana)], [("1069", True)])
        self.assertEqual(self.state(self.ana).handle, "5555")
        self.assertEqual(self.db.get(models.User, self.ana.id).cses_id, "5555")

    # items added afterwards -----------------------------------------------------------------------------------------

    async def test_an_item_added_later_picks_up_what_was_imported(self):
        await self.run_import(self.ana, "4242", ["1068"], count=10)
        item = self.item(self.assignment, "cses", "problem", "1068")
        await sync_item(item.id, self.db)
        self.db.expire_all()
        self.assertEqual(self.db.get(models.AssignmentItem, item.id).sync_status, "done")
        self.assertTrue(self.db.query(models.Result).filter_by(assignment_item_id=item.id, user_id=self.ana.id).one().solved)
        self.assertEqual(self.db.query(models.Result).filter_by(assignment_item_id=item.id, user_id=self.bob.id).count(), 0)

    async def test_the_profile_list_links_to_the_task_on_cses(self):
        await self.run_import(self.ana, "4242", ["1068"], count=10, now=NOW)
        await self.run_import(self.ana, "4242", ["1068", "1083"], count=11, now=NOW + 3600)
        sub = submissions.recent(self.db, self.ana.id)[0]
        from app.platforms import registry
        platform = registry.get("cses")
        self.assertEqual(platform.submission_problem_url(sub), "https://cses.fi/problemset/task/1083")
        self.assertEqual(platform.submission_problem_label(sub), "CSES 1083")


class CsesWeb(SiteTestCase):
    def setUp(self):
        super().setUp()
        self.owner = self.make_user("coach")
        self.gid, _ = self.new_group(self.owner, "Team")
        self.ana = self.make_user("ana", groups=[self.gid])

    def post(self, client, body, **kw):
        return client.post("/cses/import", content=json.dumps(body), headers={"Content-Type": "application/json"}, **kw)

    def test_the_page_needs_a_sign_in_and_explains_how_to_import(self):
        r = TestClient(app, follow_redirects=False).get("/cses")
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/login?next=%2Fcses"))
        page = self.ana.get("/cses").text
        self.assertIn("Not linked yet", page)
        self.assertIn('id="bookmarklet"', page)
        self.assertIn('<script src="/static/cses-export.js">', page)
        self.assertIn('href="/cses">CSES</a>', page)                      # in the menu

    def test_importing_through_the_endpoint_links_the_account_and_the_page_says_so(self):
        with profile(25):
            r = self.post(self.ana, {"cses_id": "4242", "solved": ["1068", "1083"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"cses_id": "4242", "name": "Ana", "solved": 2, "new": 2, "baseline": True})
        page = self.ana.get("/cses").text
        self.assertIn("Linked to CSES user", page)
        self.assertIn(">4242</a>", page)
        self.assertIn("2 solved tasks recorded", page)
        db = SessionLocal()
        event = db.query(models.AuditEvent).filter_by(action="cses.import").one()
        details = json.loads(event.details) if isinstance(event.details, str) else event.details
        db.close()
        self.assertEqual((event.actor_name, details["cses_id"], details["first_import"]), ("ana", "4242", True))

    def test_a_refusal_is_a_json_error_with_the_right_status(self):
        with profile(1):
            r = self.post(self.ana, {"cses_id": "4242", "solved": ["1", "2"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("only 1 submissions", r.json()["error"])
        self.assertEqual(self.post(self.ana, {"cses_id": "4242", "solved": "x"}).status_code, 400)
        self.assertEqual(self.ana.post("/cses/import", content="not json", headers={"Content-Type": "application/json"}).status_code, 400)
        self.assertEqual(self.ana.post("/cses/import", content="[1]", headers={"Content-Type": "application/json"}).status_code, 400)

    def test_the_cooldown_answers_429(self):
        with profile(10):
            self.assertEqual(self.post(self.ana, {"cses_id": "4242", "solved": ["1"]}).status_code, 200)
            r = self.post(self.ana, {"cses_id": "4242", "solved": ["1"]})
        self.assertEqual(r.status_code, 429)

    def test_only_signed_in_non_admin_accounts_can_import(self):
        anonymous = TestClient(app, follow_redirects=False)
        self.assertIn(self.post(anonymous, {"cses_id": "1", "solved": []}).status_code, (303, 401))
        self.assertEqual(self.post(self.admin, {"cses_id": "1", "solved": []}).status_code, 403)

    def add_items(self, *specs):
        r = self.owner.post("/assignments/new", data={"group_id": self.gid, "title": "Week 1"})
        aid = int(re.search(r"/assignments/(\d+)", r.headers["location"]).group(1))
        with mock.patch("app.routers.assignments._run_sync", mock.AsyncMock()):
            for platform, external_id in specs:
                self.owner.post(f"/assignments/{aid}/items/add",
                                data={"item_type": "problem", "platform": platform, "external_id": external_id})
        db = SessionLocal()
        ids = [i.id for i in db.query(models.AssignmentItem).order_by(models.AssignmentItem.id)]
        user_id = db.query(models.User).filter_by(username="ana").one().id
        db.close()
        return aid, ids, user_id

    def mark(self, client, aid, item_id, user_id, solved="1"):
        return client.post(f"/assignments/{aid}/items/{item_id}/solved", data={"user_id": user_id, "solved": solved})

    def test_before_importing_the_circle_is_clickable_and_says_what_it_does_not_do(self):
        aid, (cses_item, pbinfo_item), uid = self.add_items(("cses", "1068"), ("pbinfo", "1"))
        page = self.ana.get(f"/assignments/{aid}").text
        self.assertEqual(page.count('<button class="solve-toggle'), 2)           # one circle per manual row (CSES, pbinfo)
        cses_title = re.search(r'title="(Not marked\. Click to mark as solved\.[^"]*rankings[^"]*)"', page)
        self.assertIsNotNone(cses_title)                                   # the CSES circle explains itself
        self.assertEqual(len(re.findall(r'title="Not marked\. Click to mark as solved\."', page)), 1)  # pbinfo's doesn't
        self.assertEqual(self.mark(self.ana, aid, cses_item, uid).status_code, 303)  # and marking works

    def test_after_importing_the_cell_is_a_locked_tick_and_marking_by_hand_is_refused(self):
        aid, (solved_item, unsolved_item, pbinfo_item), uid = self.add_items(("cses", "1068"), ("cses", "1090"), ("pbinfo", "1"))
        with profile(25):
            self.assertEqual(self.post(self.ana, {"cses_id": "4242", "solved": ["1068"]}).status_code, 200)
        for viewer in (self.ana, self.owner):                              # the member herself and the group's owner
            page = self.page_cells(viewer, aid)
            self.assertIn("Solved on CSES (from the last import)", page)
            self.assertIn("Not solved on CSES at the last import", page)
            self.assertNotIn(f'name="user_id" value="{uid}"', self.cses_rows(page))  # no form left for her CSES rows
        refused = self.mark(self.ana, aid, unsolved_item, uid)
        self.assertEqual(refused.status_code, 400)
        self.assertIn("import", refused.text)
        self.assertEqual(self.mark(self.owner, aid, unsolved_item, uid).status_code, 400)  # not even the owner
        self.assertEqual(self.mark(self.ana, aid, pbinfo_item, uid).status_code, 303)      # other platforms: still by hand
        db = SessionLocal()
        self.assertEqual(db.query(models.Result).filter_by(assignment_item_id=unsolved_item).count(), 0)
        db.close()

    def page_cells(self, client, aid):
        return client.get(f"/assignments/{aid}").text

    def cses_rows(self, page):
        """The matrix rows that belong to CSES tasks (each row is one <tr>)."""
        return "".join(r for r in re.findall(r"<tr.*?</tr>", page, re.S) if "cses.fi/problemset/task" in r)

    def test_a_group_member_sees_the_solve_in_the_matrix(self):
        r = self.owner.post("/assignments/new", data={"group_id": self.gid, "title": "Week 1"})
        aid = int(re.search(r"/assignments/(\d+)", r.headers["location"]).group(1))
        with mock.patch("app.routers.assignments._run_sync", mock.AsyncMock()):
            self.owner.post(f"/assignments/{aid}/items/add",
                            data={"item_type": "problem", "platform": "cses", "external_id": "1068"})
        before = self.ana.get(f"/assignments/{aid}").text
        with profile(10):
            self.post(self.ana, {"cses_id": "4242", "solved": ["1068"]})
        after = self.ana.get(f"/assignments/{aid}").text
        self.assertNotEqual(before, after)
        db = SessionLocal()
        self.assertTrue(db.query(models.Result).one().solved)
        db.close()


class UpgradingAnOldDatabase(unittest.TestCase):
    def test_the_new_columns_are_added_and_one_cses_account_stays_one_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db").replace("\\", "/")
            con = sqlite3.connect(path)
            con.executescript("""
                CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(50), password_hash VARCHAR(200));
                CREATE TABLE submissions (id INTEGER PRIMARY KEY, user_id INTEGER, platform VARCHAR(15), submitted_at BIGINT);
                INSERT INTO users VALUES (1, 'a', 'x'), (2, 'b', 'y');
                INSERT INTO submissions VALUES (1, 1, 'codeforces', 1700000000);
            """)
            con.commit()
            con.close()
            code = ("from app.database import ensure_columns, engine\n"
                    "from sqlalchemy import text\n"
                    "ensure_columns(); ensure_columns()\n"
                    "with engine.begin() as c:\n"
                    "    print(c.execute(text('SELECT cses_id FROM users')).all())\n"
                    "    print(c.execute(text('SELECT backfilled FROM submissions')).all())\n"
                    "    c.execute(text(\"UPDATE users SET cses_id = '7' WHERE id = 1\"))\n"
                    "    try:\n"
                    "        c.execute(text(\"UPDATE users SET cses_id = '7' WHERE id = 2\"))\n"
                    "        print('DUPLICATE ACCEPTED')\n"
                    "    except Exception as e:\n"
                    "        print('duplicate refused')\n")
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                 env={**os.environ, "DATABASE_URL": "sqlite:///" + path}, cwd=os.getcwd())
            self.assertEqual(out.returncode, 0, out.stderr[-500:])
            self.assertEqual(out.stdout.splitlines(), ["[(None,), (None,)]", "[(0,)]", "duplicate refused"])


@unittest.skipUnless(shutil.which("node"), "node is needed to run the bookmarklet")
class Bookmarklet(unittest.TestCase):
    """static/cses-export.js is what runs on cses.fi, so it is run here, exactly as the import page builds it."""

    LOGGED_OUT = ('<ul class="task-list"><li class="task"><a href="/problemset/task/1068">Weird Algorithm</a>'
                  '<span class="detail">183175 / 191237</span> <span class="task-score icon "></span>'
                  '<li class="task"><a href="/problemset/task/1083">Missing Number</a>'
                  '<span class="detail">158022 / 165103</span> <span class="task-score icon "></span></ul>')

    def run_page(self, page):
        script = """
          const fs = require('fs'), vm = require('vm');
          const source = fs.readFileSync('static/cses-export.js', 'utf8');
          const opened = [];
          const context = { document: { documentElement: { outerHTML: process.argv[1] } },
                            window: { open: (url, target) => opened.push([url, target]) },
                            JSON, encodeURIComponent };
          // exactly how /cses turns the function into a bookmarklet, and how the browser then runs it
          const fn = vm.runInNewContext(source + '; csesExport', context);
          const href = 'javascript:' + encodeURIComponent('(' + fn.toString() + ')(' + JSON.stringify('https://squad.example') + ')');
          vm.runInNewContext(decodeURIComponent(href.slice('javascript:'.length)), context);
          console.log(JSON.stringify(opened));
        """
        out = subprocess.run(["node", "-e", script, page], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr[-600:])
        (url, target), = json.loads(out.stdout)
        self.assertEqual(target, "_blank")
        self.assertTrue(url.startswith("https://squad.example/cses#"))
        from urllib.parse import unquote
        return json.loads(unquote(url.split("#", 1)[1]))

    def test_nothing_is_marked_when_logged_out(self):
        self.assertEqual(self.run_page(self.LOGGED_OUT), {"solved": [], "total": 2, "cses_id": None})

    def test_solved_tasks_are_the_ones_marked_full(self):
        page = self.LOGGED_OUT.replace('<span class="task-score icon "></span>', '<span class="task-score icon full"></span>', 1)
        self.assertEqual(self.run_page(page)["solved"], ["1068"])

    def test_attempted_or_partial_tasks_are_not_solved_and_a_browser_serialised_page_works_too(self):
        page = ('<ul><li class="task"><a href="/problemset/task/1">A</a><span class="task-score icon zero"></span></li>'
                '<li class="task"><a href="/problemset/task/2">B</a><span class="task-score icon full"></span></li>'
                '<li class="task"><a href="/problemset/task/3">C</a><span class="task-score icon"></span></li>'
                '<li class="task"><a href="/problemset/task/4">D</a><span class="task-score icon full"></span></li></ul>'
                '<a href="/user/4242">me</a>')
        data = self.run_page(page)
        self.assertEqual((data["solved"], data["total"], data["cses_id"]), (["2", "4"], 4, "4242"))

    def test_a_task_without_a_score_mark_does_not_borrow_the_next_ones(self):
        page = ('<ul><li class="task"><a href="/problemset/task/1">A</a></li>'
                '<li class="task"><a href="/problemset/task/2">B</a><span class="task-score icon full"></span></li></ul>')
        self.assertEqual(self.run_page(page)["solved"], ["2"])

    def test_a_page_that_is_not_the_task_list_finds_nothing(self):
        self.assertEqual(self.run_page("<html><body>hello</body></html>"), {"solved": [], "total": 0, "cses_id": None})


if __name__ == "__main__":
    unittest.main()
