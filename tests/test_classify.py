"""The live / virtual / upsolved logic must be exactly what it was before the submission store.

Random submission histories are classified twice: by the original code (tests/legacy_reference.py, fed API-shaped
dicts) and by the new code (fed the rows the store would hold). Any difference fails the test.
"""
import random
import unittest

from app.platforms import atc, cf
from app.platforms.base import SubmissionData
from tests import legacy_reference as legacy
from tests.support import ac_raw, cf_raw

CF_VERDICTS = ["OK", "OK", "WRONG_ANSWER", "WRONG_ANSWER", "TIME_LIMIT_EXCEEDED", "RUNTIME_ERROR",
               "COMPILATION_ERROR", "MEMORY_LIMIT_EXCEEDED", "SKIPPED", "TESTING", "PARTIAL", "CHALLENGED",
               "IDLENESS_LIMIT_EXCEEDED", "FAILED", "PRESENTATION_ERROR", None]
CF_PTYPES = ["CONTESTANT", "VIRTUAL", "PRACTICE", "PRACTICE", "OUT_OF_COMPETITION", "MANAGER"]


def new_cf(raws):
    return cf.classify_contest([cf._convert(r) for r in raws])


class ClassifyCodeforces(unittest.TestCase):
    def assertSame(self, raws, msg=""):
        expected = legacy._classify_submissions(list(raws))
        got = new_cf(raws)
        self.assertEqual(got, expected, msg)

    def test_random_histories_match_the_original(self):
        rng = random.Random(20260924)
        for case in range(3000):
            n = rng.randint(0, 25)
            times = rng.sample(range(1_600_000_000, 1_600_000_000 + 10 * n + 50), n)  # distinct: ties have no defined order
            raws = [
                cf_raw(1000 + i, 1234, rng.choice("ABCDEF"), rng.choice(CF_VERDICTS),
                       at=times[i], ptype=rng.choice(CF_PTYPES))
                for i in range(n)
            ]
            rng.shuffle(raws)  # the API returns newest first; the classifiers must not depend on input order
            self.assertSame(raws, f"case {case}")

    def test_typical_contest_weekend(self):
        raws = [
            cf_raw(1, 1, "A", "WRONG_ANSWER", at=100, ptype="CONTESTANT"),
            cf_raw(2, 1, "A", "OK", at=200, ptype="CONTESTANT"),
            cf_raw(3, 1, "B", "TIME_LIMIT_EXCEEDED", at=300, ptype="CONTESTANT"),
            cf_raw(4, 1, "B", "OK", at=5000, ptype="PRACTICE"),       # upsolved
            cf_raw(5, 1, "C", "OK", at=400, ptype="VIRTUAL"),
            cf_raw(6, 1, "D", "WRONG_ANSWER", at=500, ptype="PRACTICE"),
        ]
        result, live, virtual = new_cf(raws)
        self.assertEqual((live, virtual), (True, True))
        self.assertEqual(result["A"], {"solved": True, "solve_type": "live", "attempts": 1, "wrong_verdicts": ["WA"]})
        self.assertEqual(result["B"], {"solved": True, "solve_type": "upsolving", "attempts": 0, "wrong_verdicts": []})
        self.assertEqual(result["C"]["solve_type"], "virtual")
        self.assertEqual(result["D"], {"solved": False, "solve_type": None, "attempts": 1, "wrong_verdicts": ["WA"]})
        self.assertSame(raws)

    def test_practice_only_is_standalone_not_upsolving(self):
        raws = [cf_raw(1, 1, "A", "OK", at=10, ptype="PRACTICE")]
        self.assertEqual(new_cf(raws)[0]["A"]["solve_type"], "standalone")
        self.assertSame(raws)

    def test_most_recent_solve_wins(self):
        raws = [
            cf_raw(1, 1, "A", "OK", at=100, ptype="CONTESTANT"),
            cf_raw(2, 1, "A", "OK", at=900, ptype="PRACTICE"),
        ]
        self.assertEqual(new_cf(raws)[0]["A"]["solve_type"], "upsolving")
        self.assertSame(raws)

    def test_team_virtual_participation_counts_as_virtual(self):
        raws = [cf_raw(1, 105427, "A", "OK", at=100, ptype="VIRTUAL", team=(226957, "[UAIC] paul_diac_goat"))]
        result, live, virtual = new_cf(raws)
        self.assertEqual((live, virtual, result["A"]["solve_type"]), (False, True, "virtual"))
        self.assertSame(raws)

    def test_empty(self):
        self.assertEqual(new_cf([]), ({}, False, False))
        self.assertSame([])

    def test_pending_verdicts_are_kept_as_the_original_did(self):
        raws = [cf_raw(1, 1, "A", "TESTING", at=10), cf_raw(2, 1, "A", None, at=20), cf_raw(3, 1, "A", "SKIPPED", at=30)]
        self.assertSame(raws)


class ClassifyAtCoder(unittest.TestCase):
    START, END = 1_000_000, 1_006_000

    def new(self, raws, pids, had_rated):
        rows = [
            SubmissionData(submission_id=r["id"], problem_key=r["problem_id"], submitted_at=r["epoch_second"],
                           verdict=r["result"], accepted=r["result"] == "AC")
            for r in raws
        ]
        return atc.classify_contest(rows, pids, self.START, self.END, had_rated)

    def old(self, raws, pids, had_rated):
        return legacy._classify_ac_submissions(list(raws), pids, self.START, self.END, had_rated)

    def test_random_histories_match_the_original(self):
        rng = random.Random(77)
        pids = {f"abc100_{c}" for c in "abcdefg"}
        others = {"abc101_a", "abc099_b"}
        results = ["AC", "AC", "WA", "WA", "TLE", "RE", "CE", "MLE", "OLE", "IE", "WJ", None]
        for case in range(3000):
            n = rng.randint(0, 25)
            # times straddle the contest window, including its exact edges
            pool = list(range(self.START - 3000, self.END + 3000)) + [self.START, self.END, self.END - 1]
            times = rng.sample(sorted(set(pool)), n)
            raws = [
                ac_raw(10 + i, rng.choice(sorted(pids | others)), rng.choice(results), at=times[i])
                for i in range(n)
            ]
            rng.shuffle(raws)
            had_rated = rng.random() < 0.4
            self.assertEqual(self.new(raws, pids, had_rated), self.old(raws, pids, had_rated), f"case {case}")

    def test_live_upsolve_standalone_and_pre_solved(self):
        pids = {"abc100_a", "abc100_b", "abc100_c", "abc100_d"}
        raws = [
            ac_raw(1, "abc100_a", "WA", at=self.START + 10),
            ac_raw(2, "abc100_a", "AC", at=self.START + 20),                # live
            ac_raw(3, "abc100_b", "AC", at=self.END + 500),                 # upsolve (participated)
            ac_raw(4, "abc100_c", "AC", at=self.START - 5000),              # solved long before
            ac_raw(5, "abc100_d", "WA", at=self.START + 30),                # unsolved
        ]
        result, participated = self.new(raws, pids, False)
        self.assertTrue(participated)
        self.assertEqual(result["abc100_a"], {"solved": True, "solve_type": "live", "attempts": 1, "wrong_verdicts": ["WA"]})
        self.assertEqual(result["abc100_b"]["solve_type"], "upsolving")
        self.assertEqual(result["abc100_c"]["solve_type"], "standalone")
        self.assertFalse(result["abc100_d"]["solved"])
        self.assertEqual(result, self.old(raws, pids, False)[0])


if __name__ == "__main__":
    unittest.main()
