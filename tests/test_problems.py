"""Source detection and link parsing (app/problems.py and the platforms' parse_* methods)."""
import unittest

from app import problems
from app.platforms import registry


def parse(token):
    link, reason = problems.parse_link(token)
    return (link.platform, link.type, link.external_id, link.source_url) if link else ("ERR", reason)


class DetectSource(unittest.TestCase):
    CASES = [
        # regular Codeforces
        ("https://codeforces.com/contest/2085", problems.CF),
        ("https://codeforces.com/contest/1234/problem/A", problems.CF),
        ("https://codeforces.com/problemset/problem/1234/A", problems.CF),
        ("codeforces.com/contest/1", problems.CF),
        ("www.codeforces.com/contest/1", problems.CF),
        ("https://m1.codeforces.com/contest/1", problems.CF),
        # gyms
        ("https://codeforces.com/gym/105427", problems.CF_GYM),
        ("https://codeforces.com/gym/105427/problem/A", problems.CF_GYM),
        ("https://codeforces.com/problemset/gymProblem/105427/A", problems.CF_GYM),
        # EDU
        ("https://codeforces.com/edu/course/2/lesson/4/3/practice/contest/274545/problem/A", problems.CF_EDU),
        ("https://codeforces.com/edu/courses", problems.CF_EDU),
        # others
        ("https://atcoder.jp/contests/abc400/tasks/abc400_c", problems.ATC),
        ("https://atcoder.jp/contests/abc400", problems.ATC),
        ("https://kilonova.ro/problems/2460", problems.KN),
        ("https://kilonova.ro/problem_lists/1572", problems.KN),
        ("https://cses.fi/problemset/task/1068", problems.CSES),
        # bare ids
        ("1234A", problems.CF),
        ("1234/A", problems.CF),
        ("abc400_c", problems.ATC),
        # default case
        ("https://example.com/problem/1", problems.UNKNOWN),
        ("https://leetcode.com/problems/two-sum/", problems.UNKNOWN),
        ("hello", problems.UNKNOWN),
        ("12345", problems.UNKNOWN),
    ]

    def test_every_source(self):
        for token, expected in self.CASES:
            with self.subTest(token=token):
                self.assertEqual(problems.detect_source(token), expected)

    def test_a_lookalike_host_is_not_codeforces(self):
        self.assertEqual(problems.detect_source("https://evilcodeforces.com/contest/1"), problems.UNKNOWN)
        self.assertEqual(problems.detect_source("https://codeforces.com.evil.io/contest/1"), problems.UNKNOWN)
        self.assertEqual(problems.detect_source("https://notatcoder.jp/contests/abc1"), problems.UNKNOWN)

    def test_every_source_maps_to_a_registered_platform(self):
        for source, key in problems.SOURCE_PLATFORM.items():
            self.assertIn(key, registry.PLATFORMS, source)


class ParseLink(unittest.TestCase):
    def test_codeforces(self):
        self.assertEqual(parse("https://codeforces.com/contest/2085"), ("codeforces", "contest", "2085", None))
        self.assertEqual(parse("https://codeforces.com/contest/2085/standings"), ("codeforces", "contest", "2085", None))
        self.assertEqual(parse("https://codeforces.com/contest/1234/problem/a"), ("codeforces", "problem", "1234/A", None))
        self.assertEqual(parse("https://codeforces.com/problemset/problem/1234/B2"), ("codeforces", "problem", "1234/B2", None))
        self.assertEqual(parse("1234a"), ("codeforces", "problem", "1234/A", None))
        self.assertEqual(parse("1234/A"), ("codeforces", "problem", "1234/A", None))

    def test_gym_problem_is_a_problem_not_a_contest(self):
        # the old parser turned /gym/N/problem/X into "contest N"
        self.assertEqual(parse("https://codeforces.com/gym/105427/problem/C"), ("codeforces", "problem", "105427/C", None))
        self.assertEqual(parse("https://codeforces.com/problemset/gymProblem/105427/C"), ("codeforces", "problem", "105427/C", None))
        self.assertEqual(parse("https://codeforces.com/gym/105427"), ("codeforces", "contest", "105427", None))
        self.assertEqual(parse("https://codeforces.com/gym/105427/standings"), ("codeforces", "contest", "105427", None))

    def test_edu_problem_keeps_its_link(self):
        url = "https://codeforces.com/edu/course/2/lesson/4/3/practice/contest/274545/problem/A"
        self.assertEqual(parse(url), ("codeforces", "problem", "274545/A", url))
        self.assertEqual(parse(url.replace("/problem/A", "/problem/b"))[3], url.replace("/problem/A", "/problem/B"))

    def test_edu_page_that_is_not_a_problem_is_rejected_with_a_hint(self):
        r = parse("https://codeforces.com/edu/course/2")
        self.assertEqual(r[0], "ERR")
        self.assertIn("single problem", r[1])

    def test_atcoder(self):
        self.assertEqual(parse("https://atcoder.jp/contests/abc400/tasks/abc400_c"), ("atcoder", "problem", "abc400_c", None))
        self.assertEqual(parse("https://atcoder.jp/contests/abc400"), ("atcoder", "contest", "abc400", None))
        self.assertEqual(parse("https://atcoder.jp/contests/adt_hard_20240502_2"), ("atcoder", "contest", "adt_hard_20240502_2", None))
        self.assertEqual(parse("abc400_c"), ("atcoder", "problem", "abc400_c", None))
        self.assertEqual(parse("https://atcoder.jp/contests/archive")[0], "ERR")

    def test_kilonova(self):
        self.assertEqual(parse("https://kilonova.ro/problems/2460"), ("kilonova", "problem", "2460", None))
        self.assertEqual(parse("https://kilonova.ro/problem_lists/1572"), ("kilonova", "contest", "1572", None))
        self.assertEqual(parse("https://kilonova.ro/contests/12")[0], "ERR")

    def test_cses(self):
        self.assertEqual(parse("https://cses.fi/problemset/task/1068"), ("cses", "problem", "1068", None))
        self.assertEqual(parse("https://cses.fi/problemset/task/1144/"), ("cses", "problem", "1144", None))
        self.assertEqual(parse("https://cses.fi/problemset/view/1734"), ("cses", "problem", "1734", None))
        r = parse("https://cses.fi/problemset/list/")
        self.assertEqual(r[0], "ERR")
        self.assertIn("single task", r[1])

    def test_unknown_links_are_kept(self):
        r = parse("https://leetcode.com/problems/two-sum/")
        self.assertEqual(r[:2], ("other", "problem"))
        self.assertTrue(r[2].startswith("url:"))
        self.assertEqual(r[3], "https://leetcode.com/problems/two-sum/")
        # the same link always gives the same id, so duplicates can be spotted
        self.assertEqual(parse("https://leetcode.com/problems/two-sum/"), r)
        self.assertNotEqual(parse("https://leetcode.com/problems/three-sum/")[2], r[2])
        self.assertEqual(parse("www.example.com/x")[3], "https://www.example.com/x")

    def test_unusable_input_is_rejected(self):
        for token in ("", "12345", "hello", "javascript:alert(1)", "data:text/html,x", "ftp://example.com/x",
                      "https://localhost/x", "https://example.com/" + "a" * 600):
            with self.subTest(token=token[:40]):
                self.assertEqual(parse(token)[0], "ERR")

    def test_ambiguous_number(self):
        self.assertIn("ambiguous", parse("12345")[1])

    def test_punctuation_around_links_is_ignored(self):
        self.assertEqual(parse("(https://codeforces.com/contest/2085)."), ("codeforces", "contest", "2085", None))
        self.assertEqual(parse('"https://cses.fi/problemset/task/1068",')[:3], ("cses", "problem", "1068"))


class ParseLinks(unittest.TestCase):
    def test_bulk_paste_of_mixed_links(self):
        text = ("https://codeforces.com/edu/course/2/lesson/4/3/practice/contest/274545/problem/A\n"
                "https://cses.fi/problemset/task/1144/, https://cses.fi/problemset/task/1734\n"
                "https://codeforces.com/gym/105427/problem/A https://codeforces.com/gym/105427\n"
                "https://example.org/some/problem abc400_c 1234A")
        links, errors = problems.parse_links(text)
        self.assertEqual(errors, [])
        self.assertEqual([(l.platform, l.type, l.external_id) for l in links], [
            ("codeforces", "problem", "274545/A"),
            ("cses", "problem", "1144"),
            ("cses", "problem", "1734"),
            ("codeforces", "problem", "105427/A"),
            ("codeforces", "contest", "105427"),
            ("other", "problem", links[5].external_id),
            ("atcoder", "problem", "abc400_c"),
            ("codeforces", "problem", "1234/A"),
        ])

    def test_duplicates_collapse_and_errors_are_reported_per_token(self):
        links, errors = problems.parse_links("1234A 1234/a 999 nonsense https://cses.fi/problemset/list/")
        self.assertEqual(len(links), 1)
        self.assertEqual([t for t, _ in errors], ["999", "nonsense", "https://cses.fi/problemset/list/"])


class ParseForm(unittest.TestCase):
    """The add-item form: the platform is chosen by the user, so the input is interpreted by that platform."""

    def check(self, platform, item_type, raw, expected):
        link, error = problems.parse_form(platform, item_type, raw)
        got = (link.platform, link.type, link.external_id, link.source_url) if link else ("ERR",)
        self.assertEqual(got, expected, (platform, item_type, raw, error))

    def test_codeforces(self):
        self.check("codeforces", "contest", "2085", ("codeforces", "contest", "2085", None))
        self.check("codeforces", "contest", "https://codeforces.com/contest/2085", ("codeforces", "contest", "2085", None))
        self.check("codeforces", "contest", "https://codeforces.com/gym/105427", ("codeforces", "contest", "105427", None))
        self.check("codeforces", "contest", "abc", ("ERR",))
        self.check("codeforces", "problem", "2085a", ("codeforces", "problem", "2085/A", None))
        self.check("codeforces", "problem", "https://codeforces.com/contest/1234/problem/A", ("codeforces", "problem", "1234/A", None))
        self.check("codeforces", "problem", "https://codeforces.com/gym/105427/problem/K", ("codeforces", "problem", "105427/K", None))
        self.check("codeforces", "problem", "codeforces.com/problemset/problem/1234/B", ("codeforces", "problem", "1234/B", None))
        url = "https://codeforces.com/edu/course/2/lesson/4/3/practice/contest/274545/problem/A"
        self.check("codeforces", "problem", url, ("codeforces", "problem", "274545/A", url))
        self.check("codeforces", "problem", "https://codeforces.com/contest/1234", ("ERR",))  # a contest is not a problem
        self.check("codeforces", "problem", "nonsense", ("ERR",))

    def test_atcoder(self):
        self.check("atcoder", "contest", "abc400", ("atcoder", "contest", "abc400", None))
        self.check("atcoder", "contest", "https://atcoder.jp/contests/adt_hard_20240502_2", ("atcoder", "contest", "adt_hard_20240502_2", None))
        self.check("atcoder", "problem", "https://atcoder.jp/contests/abc123/tasks/abc123_a", ("atcoder", "problem", "abc123_a", None))
        self.check("atcoder", "problem", "abc123_a", ("atcoder", "problem", "abc123_a", None))
        self.check("atcoder", "problem", "###", ("ERR",))

    def test_kilonova(self):
        self.check("kilonova", "problem", "2460", ("kilonova", "problem", "2460", None))
        self.check("kilonova", "problem", "https://kilonova.ro/problems/2460", ("kilonova", "problem", "2460", None))
        self.check("kilonova", "contest", "1572", ("kilonova", "contest", "1572", None))
        self.check("kilonova", "contest", "https://kilonova.ro/problem_lists/1572", ("kilonova", "contest", "1572", None))
        self.check("kilonova", "problem", "abc", ("ERR",))

    def test_cses(self):
        self.check("cses", "problem", "1068", ("cses", "problem", "1068", None))
        self.check("cses", "problem", "https://cses.fi/problemset/task/1068", ("cses", "problem", "1068", None))
        self.check("cses", "contest", "1068", ("ERR",))
        self.check("cses", "problem", "https://example.com/x", ("ERR",))

    def test_other(self):
        self.check("other", "problem", "https://example.com/p/1", ("other", "problem", parse("https://example.com/p/1")[2], "https://example.com/p/1"))
        self.check("other", "problem", "javascript:alert(1)", ("ERR",))
        self.check("other", "problem", "not a link", ("ERR",))
        self.check("other", "contest", "https://example.com/p/1", ("ERR",))

    def test_unknown_platform_or_type(self):
        self.check("spoj", "problem", "x", ("ERR",))
        self.check("codeforces", "round", "2085", ("ERR",))


if __name__ == "__main__":
    unittest.main()
