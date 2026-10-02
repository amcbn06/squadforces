"""A group's "Resources" switch and the per-assignment dropdown it adds: who can write, edit and read, and that
Markdown renders with links clickable but nothing a user types ever becoming live markup."""
import re
import unittest

from app import models
from app.database import SessionLocal
from app.templating import render_markdown
from tests.test_invites_groups import SiteTestCase


class Markdown(unittest.TestCase):
    def test_formatting_renders(self):
        out = str(render_markdown("**bold**, `code`\n\n- one\n- two"))
        for part in ("<strong>bold</strong>", "<code>code</code>", "<ul>", "<li>one</li>"):
            self.assertIn(part, out)

    def test_links_open_in_a_new_tab_and_bare_urls_are_linked(self):
        out = str(render_markdown("[docs](https://a.example) and https://b.example/x."))
        self.assertIn('<a href="https://a.example" target="_blank" rel="noopener noreferrer">docs</a>', out)
        self.assertIn('href="https://b.example/x"', out)
        self.assertIn("</a>.", out)  # the full stop stays outside the link

    def test_raw_html_is_shown_as_text(self):
        out = str(render_markdown("<script>alert(1)</script> <img src=x onerror=alert(1)>"))
        self.assertNotIn("<script", out)
        self.assertNotIn("<img", out)
        self.assertIn("&lt;script&gt;", out)

    def test_dangerous_link_targets_are_not_links(self):
        for bad in ("[x](javascript:alert(1))", "[x](data:text/html;base64,AAAA)", "[x](vbscript:run)"):
            self.assertNotIn("<a ", str(render_markdown(bad)), bad)

    def test_images_are_not_fetched(self):
        self.assertNotIn("<img", str(render_markdown("![x](https://a.example/p.png)")))

    def test_a_single_newline_is_a_line_break_and_none_is_empty(self):
        self.assertIn("<br", str(render_markdown("one\ntwo")))
        self.assertEqual(str(render_markdown(None)), "")


class Resources(SiteTestCase):
    def setUp(self):
        super().setUp()
        self.owner = self.make_user("coach")
        self.gid, _ = self.new_group(self.owner, resources=True)
        self.member = self.make_user("alice", groups=[self.gid])
        r = self.owner.post("/assignments/new", data={"group_id": self.gid, "title": "Week 1"})
        self.aid = int(re.search(r"/assignments/(\d+)", r.headers["location"]).group(1))

    def add(self, client, text, aid=None):
        return client.post(f"/assignments/{aid or self.aid}/resources/add", data={"text": text})

    def stored(self, aid=None):
        db = SessionLocal()
        try:
            return [r.text for r in db.query(models.Resource).filter_by(assignment_id=aid or self.aid)
                    .order_by(models.Resource.id)]
        finally:
            db.close()

    def first_id(self):
        db = SessionLocal()
        try:
            return db.query(models.Resource).order_by(models.Resource.id).first().id
        finally:
            db.close()

    def box(self, client, query=""):
        return client.get(f"/assignments/{self.aid}{query}").text.split('id="resources"')[1].split("Problem matrix")[0]

    def test_the_owner_adds_one_and_everyone_reads_it(self):
        r = self.add(self.owner, "Start with https://cp-algorithms.com/dynamic_programming/intro-to-dp.html")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], f"/assignments/{self.aid}?resources=1")
        for client in (self.owner, self.member):
            page = client.get(f"/assignments/{self.aid}").text
            self.assertIn('href="https://cp-algorithms.com/dynamic_programming/intro-to-dp.html"', page)
        # the box is opened again right after adding, and stays closed on a plain visit
        self.assertNotIn("display:none", re.search(r'class="box-body"([^>]*)>', self.box(self.owner, "?resources=1")).group(1))
        self.assertIn("display:none", re.search(r'class="box-body"([^>]*)>', self.box(self.owner)).group(1))

    def test_entries_render_as_markdown(self):
        self.add(self.owner, "**Read this** first\n\n- [intro](https://a.example)\n- `dp[i]`")
        page = self.member.get(f"/assignments/{self.aid}").text
        for part in ("<strong>Read this</strong>", '<a href="https://a.example"', "<code>dp[i]</code>", "<li>"):
            self.assertIn(part, page)

    def test_a_member_can_read_but_not_write(self):
        self.add(self.owner, "a note")
        box = self.box(self.member)
        self.assertNotIn("/resources/add", box)
        self.assertNotIn("/delete", box)
        self.assertNotIn("/edit", box)
        self.assertEqual(self.add(self.member, "sneaky").status_code, 403)
        self.assertEqual(self.stored(), ["a note"])

    def test_a_member_sees_no_box_until_there_is_something_in_it(self):
        self.assertNotIn('id="resources"', self.member.get(f"/assignments/{self.aid}").text)
        self.assertIn('id="resources"', self.owner.get(f"/assignments/{self.aid}").text)  # the owner can fill it

    def test_switched_off_means_no_box_and_no_writing(self):
        gid, _ = self.new_group(self.owner, name="Plain")  # resources off
        r = self.owner.post("/assignments/new", data={"group_id": gid, "title": "W"})
        aid = int(re.search(r"/assignments/(\d+)", r.headers["location"]).group(1))
        self.assertNotIn('id="resources"', self.owner.get(f"/assignments/{aid}").text)
        self.assertEqual(self.add(self.owner, "x", aid).status_code, 400)

    def test_switching_it_off_hides_what_was_written_without_losing_it(self):
        self.add(self.owner, "keep me")
        self.owner.post(f"/groups/{self.gid}/edit", data={"name": "G", "max_members": 10})  # resources unticked
        self.assertNotIn("keep me", self.member.get(f"/assignments/{self.aid}").text)
        self.owner.post(f"/groups/{self.gid}/edit", data={"name": "G", "max_members": 10, "resources_allowed": "1"})
        self.assertIn("keep me", self.member.get(f"/assignments/{self.aid}").text)

    def test_the_owner_edits_an_entry(self):
        self.add(self.owner, "old text")
        rid = self.first_id()
        r = self.owner.post(f"/assignments/{self.aid}/resources/{rid}/edit", data={"text": "**new** text"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], f"/assignments/{self.aid}?resources=1")
        self.assertEqual(self.stored(), ["**new** text"])
        self.assertIn("<strong>new</strong>", self.member.get(f"/assignments/{self.aid}").text)
        self.assertIn("Edited a resource on an assignment", self.owner.get(f"/groups/{self.gid}").text)

    def test_editing_is_for_the_owner_only_and_never_empties_an_entry(self):
        self.add(self.owner, "keep")
        rid = self.first_id()
        url = f"/assignments/{self.aid}/resources/{rid}/edit"
        self.assertEqual(self.member.post(url, data={"text": "hacked"}).status_code, 403)
        self.owner.post(url, data={"text": "   "})  # blank: left alone
        self.assertEqual(self.owner.post(url, data={"text": "x" * 2001}).status_code, 400)
        self.assertEqual(self.stored(), ["keep"])

    def test_editing_through_another_assignment_does_nothing(self):
        self.add(self.owner, "mine")
        rid = self.first_id()
        r = self.owner.post("/assignments/new", data={"group_id": self.gid, "title": "Week 2"})
        other = int(re.search(r"/assignments/(\d+)", r.headers["location"]).group(1))
        self.owner.post(f"/assignments/{other}/resources/{rid}/edit", data={"text": "changed"})
        self.assertEqual(self.stored(), ["mine"])

    def test_the_edit_form_is_prefilled_with_the_raw_markdown(self):
        self.add(self.owner, "a **b** <i>c</i>")
        self.assertIn("a **b** &lt;i&gt;c&lt;/i&gt;</textarea>", self.box(self.owner))

    def test_deleting_is_confirmed_inline_not_with_a_browser_popup(self):
        # A native confirm() is swallowed by some browsers and panes, which made the button look dead.
        self.add(self.owner, "x")
        box = self.box(self.owner)
        self.assertIn('id="res-confirm-', box)
        self.assertNotIn("confirm(", box)
        self.assertNotIn('id="res-confirm-', self.box(self.member))

    def test_the_owner_removes_one(self):
        self.add(self.owner, "first")
        self.add(self.owner, "second")
        rid = self.first_id()
        r = self.owner.post(f"/assignments/{self.aid}/resources/{rid}/delete")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.stored(), ["second"])
        self.assertEqual(self.member.post(f"/assignments/{self.aid}/resources/{rid}/delete").status_code, 403)

    def test_deleting_through_another_assignment_does_nothing(self):
        self.add(self.owner, "mine")
        rid = self.first_id()
        r = self.owner.post("/assignments/new", data={"group_id": self.gid, "title": "Week 2"})
        other = int(re.search(r"/assignments/(\d+)", r.headers["location"]).group(1))
        self.owner.post(f"/assignments/{other}/resources/{rid}/delete")
        self.assertEqual(self.stored(), ["mine"])

    def test_limits(self):
        self.add(self.owner, "   ")  # blank: ignored
        self.assertEqual(self.stored(), [])
        self.assertEqual(self.add(self.owner, "x" * 2001).status_code, 400)
        for i in range(50):
            self.add(self.owner, f"r{i}")
        self.assertEqual(self.add(self.owner, "one too many").status_code, 400)
        self.assertEqual(len(self.stored()), 50)

    def test_markup_in_a_resource_is_shown_as_text(self):
        self.add(self.owner, "<b>bold</b> <script>alert(1)</script>")
        page = self.member.get(f"/assignments/{self.aid}").text
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_the_group_form_has_the_switch_and_remembers_it(self):
        self.assertRegex(self.owner.get(f"/groups/{self.gid}/edit-page").text,
                         r'name="resources_allowed" value="1"\s*checked')
        self.assertNotRegex(self.owner.get("/groups/new").text, r'name="resources_allowed" value="1"\s*checked')

    def test_changes_are_in_the_audit_trail(self):
        self.add(self.owner, "audited")
        page = self.owner.get(f"/groups/{self.gid}").text
        self.assertIn("Added a resource to an assignment", page)

    def test_deleting_the_assignment_removes_its_resources(self):
        self.add(self.owner, "gone soon")
        self.owner.post(f"/assignments/{self.aid}/delete")
        self.assertEqual(self.stored(), [])


if __name__ == "__main__":
    unittest.main()
