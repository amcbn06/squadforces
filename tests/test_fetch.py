"""Each platform's fetch_submissions / rating history against faked judge APIs (paging, stopping, conversion)."""
import unittest
from datetime import datetime, timezone
from unittest import mock

from app.platforms import atc, cf, kn
from app.platforms.base import KnownState
from app.scraper import atcoder as ac_api
from app.scraper import codeforces as cf_api
from app.scraper import kilonova as kn_api
from tests.support import ac_raw, cf_raw, kn_raw


class CodeforcesConversion(unittest.TestCase):
    def test_accepted_submission(self):
        d = cf._convert(cf_raw(11, 1234, "B", "OK", at=500, ptype="CONTESTANT", rel=1300, name="Sum", rating=1400))
        self.assertEqual((d.submission_id, d.problem_key, d.contest_key, d.problem_index), (11, "1234/B", "1234", "B"))
        self.assertEqual((d.verdict, d.accepted, d.final), ("AC", True, True))
        self.assertEqual((d.submitted_at, d.relative_seconds, d.participant_type), (500, 1300, "CONTESTANT"))
        self.assertEqual((d.problem_name, d.problem_rating, d.language), ("Sum", 1400, "C++20"))
        self.assertEqual((d.team_id, d.team_name), (None, None))

    def test_verdict_codes(self):
        for raw, short in [("WRONG_ANSWER", "WA"), ("TIME_LIMIT_EXCEEDED", "TLE"), ("MEMORY_LIMIT_EXCEEDED", "MLE"),
                           ("RUNTIME_ERROR", "RE"), ("COMPILATION_ERROR", "CE"), ("CHALLENGED", "HK"),
                           ("SKIPPED", "SKI"), ("PARTIAL", "PAR")]:
            with self.subTest(raw):
                d = cf._convert(cf_raw(1, 1, "A", raw))
                self.assertEqual((d.verdict, d.accepted, d.final), (short, False, True))

    def test_submissions_still_being_judged_are_not_final(self):
        self.assertFalse(cf._convert(cf_raw(1, 1, "A", "TESTING")).final)
        self.assertFalse(cf._convert(cf_raw(1, 1, "A", None)).final)

    def test_team_submission_keeps_the_team(self):
        d = cf._convert(cf_raw(1, 105427, "A", ptype="VIRTUAL", team=(226957, "[UAIC] paul_diac_goat")))
        self.assertEqual((d.team_id, d.team_name, d.participant_type), ("226957", "[UAIC] paul_diac_goat", "VIRTUAL"))

    def test_entries_without_a_usable_problem_are_dropped(self):
        broken = cf_raw(1, 1, "A")
        del broken["problem"]["contestId"]
        self.assertIsNone(cf._convert(broken))
        self.assertEqual(len(cf._convert_all([broken, cf_raw(2, 1, "A")])), 1)

    def test_submission_contest_can_differ_from_the_problems_contest(self):
        d = cf._convert(cf_raw(1, 100, "A", contest_of_sub=5))
        self.assertEqual((d.problem_key, d.contest_key), ("100/A", "5"))

    def test_missing_participant_type_defaults_to_practice(self):
        raw = cf_raw(1, 1, "A")
        del raw["author"]["participantType"]
        self.assertEqual(cf._convert(raw).participant_type, "PRACTICE")


class CodeforcesFetch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # newest first, like user.status
        self.server = [cf_raw(sid, 1000 + sid % 7, "A") for sid in range(1000, 0, -1)]
        self.calls = []

        async def fake_status(handle, offset=1, count=100000):
            self.calls.append((handle, offset, count))
            return self.server[offset - 1: offset - 1 + count]

        p = mock.patch.object(cf_api, "get_user_status", fake_status)
        p.start()
        self.addCleanup(p.stop)

    async def test_first_load_is_one_call_for_everything(self):
        rows = await cf.PLATFORM.fetch_submissions("h", KnownState())
        self.assertEqual(len(rows), 1000)
        self.assertEqual(self.calls, [("h", 1, cf.FULL_HISTORY_COUNT)])

    async def test_catching_up_stops_once_it_reaches_the_known_submission(self):
        # stop at id 950: only the first 51 entries are newer-or-equal, so one page of 200 is enough
        rows = await cf.PLATFORM.fetch_submissions("h", KnownState(stop_id=950, stop_at=0))
        self.assertEqual(len(self.calls), 1)
        self.assertGreaterEqual(min(r.submission_id for r in rows), 801)
        self.assertIn(950, {r.submission_id for r in rows})

    async def test_catching_up_pages_back_as_far_as_needed(self):
        # id 500 sits 500 entries down: pages of 200 -> needs the 3rd page
        rows = await cf.PLATFORM.fetch_submissions("h", KnownState(stop_id=500, stop_at=0))
        self.assertEqual([c[1] for c in self.calls], [1, 201, 401])
        self.assertIn(500, {r.submission_id for r in rows})
        self.assertNotIn(1, {r.submission_id for r in rows})

    async def test_reaching_back_past_the_whole_history_ends_cleanly(self):
        rows = await cf.PLATFORM.fetch_submissions("h", KnownState(stop_id=-5, stop_at=0))
        self.assertEqual(len(rows), 1000)
        self.assertEqual([c[1] for c in self.calls], [1, 201, 401, 601, 801, 1001])  # 1000 = 5 full pages, then an empty one

    async def test_history_that_ends_exactly_on_a_page_boundary(self):
        self.server = self.server[:400]
        rows = await cf.PLATFORM.fetch_submissions("h", KnownState(stop_id=-5, stop_at=0))
        self.assertEqual(len(rows), 400)
        self.assertEqual([c[1] for c in self.calls], [1, 201, 401])  # the third page is empty

    async def test_nothing_new_costs_one_call(self):
        newest = self.server[0]["id"]
        rows = await cf.PLATFORM.fetch_submissions("h", KnownState(stop_id=newest, stop_at=0))
        self.assertEqual(len(self.calls), 1)
        self.assertGreaterEqual(len(rows), 1)  # the known one comes back too; storing it again is a no-op

    async def test_user_with_no_submissions(self):
        self.server = []
        self.assertEqual(await cf.PLATFORM.fetch_submissions("h", KnownState()), [])
        self.assertEqual(await cf.PLATFORM.fetch_submissions("h", KnownState(stop_id=5, stop_at=0)), [])

    async def test_api_errors_propagate(self):
        async def boom(*a, **k):
            raise ValueError("CF API error: handle: User with handle x not found")
        with mock.patch.object(cf_api, "get_user_status", boom):
            with self.assertRaises(ValueError):
                await cf.PLATFORM.fetch_submissions("x", KnownState())

    async def test_rating_history(self):
        async def fake_history(handle):
            return [{"contestId": 1900, "contestName": "Round 1900", "handle": handle, "rank": 55,
                     "ratingUpdateTimeSeconds": 1_700_000_000, "oldRating": 1400, "newRating": 1480}]
        with mock.patch.object(cf_api, "get_user_rating_history", fake_history):
            (entry,) = await cf.PLATFORM.fetch_rating_history("h")
        self.assertEqual((entry.contest_key, entry.rank, entry.old_rating, entry.new_rating, entry.rated_at),
                         ("1900", 55, 1400, 1480, 1_700_000_000))


class AtCoderFetch(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_asks_from_the_known_time_and_converts(self):
        seen = []

        async def fake(handle, from_epoch=0):
            seen.append((handle, from_epoch))
            return [ac_raw(1, "abc100_a", "AC", at=100, point=300.0), ac_raw(2, "abc100_b", "WA", at=200),
                    ac_raw(3, "abc100_b", "WJ", at=300)]

        with mock.patch.object(ac_api, "get_user_submissions", fake):
            rows = await atc.PLATFORM.fetch_submissions("tourist", KnownState())
            await atc.PLATFORM.fetch_submissions("tourist", KnownState(stop_id=3, stop_at=250))
        self.assertEqual(seen, [("tourist", 0), ("tourist", 250)])
        a, b, c = rows
        self.assertEqual((a.problem_key, a.contest_key, a.verdict, a.accepted, a.final, a.score), ("abc100_a", "abc100", "AC", True, True, 300.0))
        self.assertEqual((b.verdict, b.accepted, b.final), ("WA", False, True))
        self.assertEqual((c.verdict, c.final), ("WJ", False))  # still waiting for the judge

    async def test_rating_history_uses_the_contest_slug_and_hides_unrated_changes(self):
        async def fake(handle):
            return [
                {"IsRated": True, "Place": 2, "OldRating": 0, "NewRating": 2720, "Performance": 3920,
                 "ContestScreenName": "agc004.contest.atcoder.jp", "ContestName": "AtCoder Grand Contest 004",
                 "EndTime": "2016-09-04T22:50:00+09:00"},
                {"IsRated": False, "Place": 40, "OldRating": 2720, "NewRating": 2720, "Performance": 100,
                 "ContestScreenName": "abc300.contest.atcoder.jp", "ContestName": "ABC 300",
                 "EndTime": "2023-05-01T22:40:00+09:00"},
                {"IsRated": True, "ContestScreenName": "", "Place": 1},
            ]
        with mock.patch.object(ac_api, "get_rating_history", fake):
            rated, unrated = await atc.PLATFORM.fetch_rating_history("tourist")
        self.assertEqual((rated.contest_key, rated.rank, rated.old_rating, rated.new_rating, rated.performance),
                         ("agc004", 2, 0, 2720, 3920))
        self.assertEqual(rated.rated_at, int(datetime(2016, 9, 4, 13, 50, tzinfo=timezone.utc).timestamp()))
        self.assertEqual((unrated.contest_key, unrated.rank, unrated.old_rating, unrated.new_rating), ("abc300", 40, None, None))


class AtCoderApiPaging(unittest.IsolatedAsyncioTestCase):
    """app/scraper/atcoder.py::get_user_submissions pages through kenkoooo's 500-per-call limit."""

    async def test_pages_resume_inclusively_and_dedupe(self):
        # 1200 submissions, several sharing a second across page boundaries
        allsubs = [{"id": i, "epoch_second": 1000 + i // 3, "problem_id": "p", "result": "AC"} for i in range(1200)]
        calls = []

        class FakeResp:
            def __init__(self, data): self._d = data; self.status_code = 200
            def raise_for_status(self): pass
            def json(self): return self._d

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, params=None):
                calls.append(params["from_second"])
                data = [s for s in allsubs if s["epoch_second"] >= params["from_second"]][:500]
                return FakeResp(data)

        with mock.patch.object(ac_api.httpx, "AsyncClient", FakeClient):
            got = await ac_api.get_user_submissions("u", 0)
        self.assertEqual(sorted(s["id"] for s in got), list(range(1200)))
        self.assertEqual(len({s["id"] for s in got}), len(got))
        self.assertEqual(len(calls), 3)


class KilonovaFetch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.server = [kn_raw(sid, 7, 100) for sid in range(120, 0, -1)]  # newest first
        self.calls = []

        async def fake_uid(name):
            return 5 if name == "andrei" else None

        async def fake_page(user_id, offset=0):
            self.calls.append(offset)
            return self.server[offset: offset + 50], len(self.server)

        for target, fn in ((kn_api, ("get_user_id", fake_uid)), (kn_api, ("get_user_submissions", fake_page))):
            p = mock.patch.object(target, fn[0], fn[1])
            p.start()
            self.addCleanup(p.stop)

    async def test_first_load_pages_through_everything(self):
        rows = await kn.PLATFORM.fetch_submissions("andrei", KnownState())
        self.assertEqual(len(rows), 120)
        self.assertEqual(self.calls, [0, 50, 100])

    async def test_catching_up_stops_at_the_known_submission(self):
        rows = await kn.PLATFORM.fetch_submissions("andrei", KnownState(stop_id=110, stop_at=0))
        self.assertEqual(self.calls, [0])
        self.assertIn(110, {r.submission_id for r in rows})

    async def test_unknown_user_is_an_error(self):
        with self.assertRaises(ValueError):
            await kn.PLATFORM.fetch_submissions("ghost", KnownState())

    async def test_user_without_submissions(self):
        self.server = []
        self.assertEqual(await kn.PLATFORM.fetch_submissions("andrei", KnownState()), [])

    def test_conversion(self):
        full = kn._convert(kn_raw(1, 4373, 100, contest=3147))
        self.assertEqual((full.verdict, full.accepted, full.final, full.score, full.max_score), ("AC", True, True, 100.0, 100.0))
        self.assertEqual((full.problem_key, full.contest_key), ("4373", "3147"))
        expected_epoch = int(datetime(2026, 5, 9, 11, 37, 53, tzinfo=timezone.utc).timestamp())
        self.assertEqual(full.submitted_at, expected_epoch)  # +02:00 offset respected

        partial = kn._convert(kn_raw(2, 1, 60))
        self.assertEqual((partial.verdict, partial.accepted, partial.score), ("PT", False, 60.0))
        self.assertEqual(kn._convert(kn_raw(3, 1, 0, icpc="Time limit exceeded (test #3)")).verdict, "TLE")
        self.assertEqual(kn._convert(kn_raw(3, 1, 0, icpc="Memory limit exceeded")).verdict, "MLE")
        self.assertEqual(kn._convert(kn_raw(3, 1, 0, icpc="Runtime error")).verdict, "RE")
        self.assertEqual(kn._convert(kn_raw(3, 1, 0)).verdict, "WA")
        self.assertEqual(kn._convert(kn_raw(4, 1, 0, compile_error=True)).verdict, "CE")
        pending = kn._convert(kn_raw(5, 1, 0, status="working"))
        self.assertEqual((pending.verdict, pending.final, pending.accepted), ("", False, False))
        # a non-default score scale: full marks are relative to it
        self.assertTrue(kn._convert(kn_raw(6, 1, 30, scale=30)).accepted)
        self.assertFalse(kn._convert(kn_raw(7, 1, 100, scale=200)).accepted)
        self.assertIsNone(kn._convert({"id": 1}))

    def test_timestamps_with_odd_fractions_still_parse(self):
        self.assertIsNotNone(kn._epoch("2026-05-09T13:37:53.16+02:00"))
        self.assertIsNotNone(kn._epoch("2026-05-09T13:37:53.1234567+02:00"))
        self.assertIsNotNone(kn._epoch("2026-05-09T13:37:53+02:00"))


if __name__ == "__main__":
    unittest.main()
