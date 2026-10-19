"""Hints, solutions and notes are written in Markdown: the page carries each entry's rendered HTML next to its raw
text, the editor's Preview tab renders a draft through the same filter, and who may preview follows who may write."""
import json
import re
import unittest

from fastapi.testclient import TestClient

from app import models
from app.database import SessionLocal
from app.main import app
from app.templating import render_markdown
from tests.test_invites_groups import SiteTestCase


class HintsMarkdown(SiteTestCase):
    def setUp(self):
        super().setUp()
        self.owner = self.make_user("coach")
        self.gid, _ = self.new_group(self.owner, "Team", hints=True)  # hints and notes on
        self.member = self.make_user("alice", groups=[self.gid])
        self.outsider = self.make_user("eve")
        r = self.owner.post("/assignments/new", data={"group_id": self.gid, "title": "Week 1"})
        self.aid = int(re.search(r"/assignments/(\d+)", r.headers["location"]).group(1))
        self.owner.post(f"/assignments/{self.aid}/items/add",
                        data={"item_type": "problem", "platform": "cses", "external_id": "1068"})
        db = SessionLocal()
        self.item = db.query(models.AssignmentItem).one().id
        db.close()

    def add(self, client, text, **extra):
        return client.post(f"/assignments/{self.aid}/hints/add", data={"target": f"i{self.item}", "text": text, **extra})

    def hints_json(self, client):
        page = client.get(f"/assignments/{self.aid}").text
        return json.loads(re.search(r"var HINTS = (.*?);\nvar HINT_ADMIN", page, re.S).group(1))[f"i{self.item}"]

    def preview(self, client, text, aid=None):
        return client.post(f"/assignments/{aid or self.aid}/hints/preview", data={"text": text})

    # what readers see -------------------------------------------------------------------------------------------

    def test_each_entry_carries_its_rendered_html_beside_the_raw_text(self):
        self.add(self.owner, "**Think** about [DP](https://a.example)\n\n- one\n- two")
        (entry,) = self.hints_json(self.member)
        self.assertEqual(entry["text"], "**Think** about [DP](https://a.example)\n\n- one\n- two")  # the edit form needs this
        self.assertEqual(entry["html"], str(render_markdown(entry["text"])))
        for part in ("<strong>Think</strong>", 'href="https://a.example"', "<li>one</li>"):
            self.assertIn(part, entry["html"])

    def test_notes_and_solutions_render_too(self):
        self.add(self.owner, "see `dp[i]`", is_solution="1")
        self.add(self.member, "took **long**", time_minutes="40")
        kinds = {e["kind"]: e["html"] for e in self.hints_json(self.owner)}
        self.assertIn("<code>dp[i]</code>", kinds["solution"])
        self.assertIn("<strong>long</strong>", kinds["note"])

    def test_typed_markup_is_shown_as_text_and_never_lives_in_the_page(self):
        evil = "<script>alert(1)</script> <img src=x onerror=alert(1)> [x](javascript:alert(1))"
        self.add(self.member, evil)
        page = self.owner.get(f"/assignments/{self.aid}").text
        self.assertNotIn("<script>alert(1)", page)  # not as markup anywhere, even inside the embedded JSON
        self.assertNotIn("<img src=x", page)
        (entry,) = self.hints_json(self.owner)
        for bad in ("<script", "<img", 'href="javascript'):
            self.assertNotIn(bad, entry["html"])
        self.assertIn("&lt;script&gt;", entry["html"])

    def test_links_open_in_a_new_tab(self):
        self.add(self.owner, "start at https://cp-algorithms.com/dp.html")
        (entry,) = self.hints_json(self.member)
        self.assertIn('href="https://cp-algorithms.com/dp.html" target="_blank" rel="noopener noreferrer"', entry["html"])

    def test_the_page_no_longer_carries_the_plain_text_linkifier(self):
        page = self.owner.get(f"/assignments/{self.aid}").text
        self.assertNotIn("function linkify", page)
        self.assertIn("md.innerHTML = h.html", page)

    # the editor ---------------------------------------------------------------------------------------------------

    def test_the_editor_is_loaded_while_hints_or_notes_are_on(self):
        for client in (self.owner, self.member):
            page = client.get(f"/assignments/{self.aid}").text
            self.assertIn('<script src="/static/md-editor.js">', page)
            self.assertIn("ta.dataset.previewUrl = HINT_URL + 'preview'", page)
        plain, _ = self.new_group(self.owner, "Plain")  # hints, notes and resources all off
        r = self.owner.post("/assignments/new", data={"group_id": plain, "title": "W"})
        aid = int(re.search(r"/assignments/(\d+)", r.headers["location"]).group(1))
        self.assertNotIn("md-editor", self.owner.get(f"/assignments/{aid}").text)

    # the preview route ----------------------------------------------------------------------------------------------

    def test_the_preview_is_rendered_like_the_page_and_writes_nothing(self):
        draft = "**bold** [docs](https://a.example)\n\n1. one\n2. two"
        for client in (self.owner, self.member):  # the owner for a hint, a member for a note
            r = self.preview(client, draft)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.text, str(render_markdown(draft)))
        self.assertEqual(self.preview(self.owner, "").text, "")
        db = SessionLocal()
        self.assertEqual(db.query(models.Hint).count(), 0)
        db.close()

    def test_the_preview_is_as_safe_as_the_page(self):
        out = self.preview(self.member, "<script>alert(1)</script> [x](javascript:alert(1)) ![i](https://a.example/p.png)").text
        for bad in ("<script", "<img", 'href="javascript'):
            self.assertNotIn(bad, out)

    def test_the_preview_stops_at_the_length_limit(self):
        self.assertLessEqual(len(self.preview(self.owner, "a" * 9000).text), 5000 + 10)

    def test_only_those_who_may_write_can_preview(self):
        self.assertEqual(self.preview(self.outsider, "x").status_code, 403)
        self.assertIn(self.preview(TestClient(app, follow_redirects=False), "x").status_code, (303, 401))
        self.assertEqual(self.preview(self.owner, "x", aid=99999).status_code, 404)
        # hints on, notes off: only the owner writes anything, so only the owner has anything to preview
        gid, _ = self.new_group(self.owner, "Hints only", hints=True, notes=False)
        member = self.make_user("bob", groups=[gid])
        r = self.owner.post("/assignments/new", data={"group_id": gid, "title": "W"})
        aid = int(re.search(r"/assignments/(\d+)", r.headers["location"]).group(1))
        self.assertEqual(self.preview(self.owner, "x", aid=aid).status_code, 200)
        self.assertEqual(self.preview(member, "x", aid=aid).status_code, 403)
        # everything off: nothing to write
        gid, _ = self.new_group(self.owner, "Off")
        r = self.owner.post("/assignments/new", data={"group_id": gid, "title": "W"})
        aid = int(re.search(r"/assignments/(\d+)", r.headers["location"]).group(1))
        self.assertEqual(self.preview(self.owner, "x", aid=aid).status_code, 403)

    # saving ---------------------------------------------------------------------------------------------------------

    def test_blank_text_is_ignored_not_rejected(self):
        self.assertEqual(self.add(self.owner, "").status_code, 303)
        self.assertEqual(self.hints_json_or_empty(), [])
        self.add(self.owner, "keep")
        hid = self.hints_json(self.owner)[0]["id"]
        r = self.owner.post(f"/assignments/{self.aid}/hints/{hid}/edit", data={"text": ""})
        self.assertEqual(r.status_code, 303)
        self.assertEqual([e["text"] for e in self.hints_json(self.owner)], ["keep"])

    def hints_json_or_empty(self):
        page = self.owner.get(f"/assignments/{self.aid}").text
        return json.loads(re.search(r"var HINTS = (.*?);\nvar HINT_ADMIN", page, re.S).group(1)).get(f"i{self.item}", [])


if __name__ == "__main__":
    unittest.main()
