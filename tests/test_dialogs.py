"""The app asks "are you sure?" and "what title?" in its own centred panel (base.html), never with the browser's
confirm()/prompt()/alert() popups, which some browsers and embedded panes suppress (the click then looks dead)."""
import re
import unittest
from pathlib import Path

from tests.test_invites_groups import SiteTestCase

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"
NATIVE_POPUP = re.compile(r"\b(?:confirm|prompt|alert)\(")


class NoNativePopups(unittest.TestCase):
    def test_no_template_calls_a_browser_popup(self):
        offenders = []
        for path in sorted(TEMPLATES.rglob("*.html")):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if NATIVE_POPUP.search(line):
                    offenders.append(f"{path.relative_to(TEMPLATES)}:{n}: {line.strip()[:90]}")
        self.assertEqual(offenders, [], "use data-confirm / data-prompt (see base.html) instead of a native popup")

    def test_no_static_script_calls_a_browser_popup(self):
        static = Path(__file__).resolve().parent.parent / "static"
        for path in static.rglob("*.js"):
            self.assertIsNone(NATIVE_POPUP.search(path.read_text(encoding="utf-8")), path.name)


class DialogMarkup(SiteTestCase):
    def setUp(self):
        super().setUp()
        self.owner = self.make_user("coach")
        self.gid, _ = self.new_group(self.owner, resources=True)
        r = self.owner.post("/assignments/new", data={"group_id": self.gid, "title": "Week 1"})
        self.aid = int(re.search(r"/assignments/(\d+)", r.headers["location"]).group(1))

    def test_every_page_carries_the_dialog_and_its_api(self):
        page = self.owner.get(f"/groups/{self.gid}").text
        self.assertIn('id="dialog-overlay"', page)
        self.assertIn("window.confirmDialog", page)
        self.assertIn("window.promptDialog", page)

    def test_destructive_forms_ask_through_data_attributes(self):
        group = self.owner.get(f"/groups/{self.gid}").text
        self.assertIn('data-confirm-title="Delete group"', group)
        self.assertIn('data-confirm="Delete this group and all its assignments?"', group)
        assignment = self.owner.get(f"/assignments/{self.aid}").text
        self.assertIn('data-confirm-title="Delete assignment"', assignment)

    def test_deleting_a_resource_asks_in_the_panel(self):
        self.owner.post(f"/assignments/{self.aid}/resources/add", data={"text": "x"})
        page = self.owner.get(f"/assignments/{self.aid}").text
        self.assertIn('data-confirm-title="Delete resource"', page)
        self.assertIn('data-ok-label="Delete"', page)

    def test_a_username_in_a_question_cannot_inject_markup(self):
        self.make_user('x"onmouseover="alert(1)', groups=[self.gid])  # may be refused; the page must still be safe
        page = self.owner.get(f"/groups/{self.gid}").text
        self.assertNotIn('onmouseover="alert(1)"', page)


if __name__ == "__main__":
    unittest.main()
