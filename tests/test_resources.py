"""A group's "Resources" switch and the per-assignment dropdown it adds: who can write, who can read, and that links
become clickable without anything a user types ever becoming markup."""
import re
import unittest

from app import models
from app.database import SessionLocal
from app.templating import linkify
from tests.test_invites_groups import SiteTestCase


class Linkify(unittest.TestCase):
    def test_a_url_becomes_a_link_that_opens_in_a_new_tab(self):
        out = str(linkify("read https://cp-algorithms.com/x.html first"))
        self.assertIn('<a href="https://cp-algorithms.com/x.html" target="_blank" rel="noopener noreferrer">', out)

    def test_trailing_punctuation_stays_outside_the_link(self):
        out = str(linkify("see https://a.example/page."))
        self.assertIn('href="https://a.example/page"', out)
        self.assertTrue(out.endswith("</a>."))

    def test_markup_is_escaped_everywhere(self):
        out = str(linkify('<script>alert(1)</script> https://a.example/?q="x"&y=1'))
        self.assertNotIn("<script>", out)
        self.assertNotIn('"x"', out)  # a quote can't break out of the href
        self.assertIn("&amp;y=1", out)

    def test_only_http_links_are_made(self):
        self.assertNotIn("<a ", str(linkify("javascript:alert(1) ftp://x.example mailto:a@b.c")))

    def test_plain_text_and_none_pass_through(self):
        self.assertEqual(str(linkify("just words")), "just words")
        self.assertEqual(str(linkify(None)), "")


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

    def stored(self):
        db = SessionLocal()
        try:
            return [r.text for r in db.query(models.Resource).filter_by(assignment_id=self.aid).order_by(models.Resource.id)]
        finally:
            db.close()

    def test_the_owner_adds_one_and_everyone_reads_it(self):
        r = self.add(self.owner, "Start with https://cp-algorithms.com/dynamic_programming/intro-to-dp.html")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], f"/assignments/{self.aid}?resources=1")
        for client in (self.owner, self.member):
            page = client.get(f"/assignments/{self.aid}").text
            self.assertIn('href="https://cp-algorithms.com/dynamic_programming/intro-to-dp.html"', page)
        # the box is opened again right after adding, and stays closed on a plain visit
        self.assertNotIn("display:none", re.search(r'id="resources".*?box-body"([^>]*)>',
                          self.owner.get(f"/assignments/{self.aid}?resources=1").text, re.S).group(1))
        self.assertIn("display:none", re.search(r'id="resources".*?box-body"([^>]*)>',
                      self.owner.get(f"/assignments/{self.aid}").text, re.S).group(1))

    def test_a_member_can_read_but_not_write(self):
        self.add(self.owner, "a note")
        page = self.member.get(f"/assignments/{self.aid}").text
        self.assertNotIn("/resources/add", page)
        self.assertNotIn("/delete", page.split('id="resources"')[1].split("Problem matrix")[0])
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

    def test_the_owner_removes_one(self):
        self.add(self.owner, "first")
        self.add(self.owner, "second")
        db = SessionLocal()
        rid = db.query(models.Resource).filter_by(text="first").one().id
        db.close()
        r = self.owner.post(f"/assignments/{self.aid}/resources/{rid}/delete")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.stored(), ["second"])
        self.assertEqual(self.member.post(f"/assignments/{self.aid}/resources/{rid}/delete").status_code, 403)

    def test_deleting_through_another_assignment_does_nothing(self):
        self.add(self.owner, "mine")
        r = self.owner.post("/assignments/new", data={"group_id": self.gid, "title": "Week 2"})
        other = int(re.search(r"/assignments/(\d+)", r.headers["location"]).group(1))
        db = SessionLocal()
        rid = db.query(models.Resource).one().id
        db.close()
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
