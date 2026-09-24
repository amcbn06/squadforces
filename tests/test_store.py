"""The submission store (app/submissions.py) against a fake judge."""
import asyncio
import time
from datetime import datetime, timedelta

from app import models, submissions
from app.platforms.base import KnownState, Platform, RatingData, SubmissionData
from tests.support import DbTestCase


class FakeJudge(Platform):
    """An in-memory judge. `rows` is its whole history; like a real one it can return more than was asked for."""
    key = "fake"
    label = "Fake"
    handle_attr = "codeforces_handle"
    has_submissions = True

    def __init__(self):
        self.rows: list[SubmissionData] = []
        self.calls: list[tuple[str, KnownState]] = []
        self.fail_with: Exception | None = None
        self.history: list[RatingData] | None = []
        self.history_fails = False
        self.delay = 0.0

    async def fetch_submissions(self, handle, known):
        self.calls.append((handle, known))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_with:
            raise self.fail_with
        if known.stop_id is None:
            return list(self.rows)
        return [r for r in self.rows if r.submission_id >= known.stop_id]

    async def fetch_rating_history(self, handle):
        if self.history_fails:
            raise RuntimeError("rating service down")
        return self.history


def sub(sid, problem="1/A", *, verdict="AC", final=True, at=None, contest="1", index="A", **kw):
    return SubmissionData(
        submission_id=sid, problem_key=problem, submitted_at=at if at is not None else 1_600_000_000 + sid,
        verdict=verdict, accepted=verdict == "AC", final=final, contest_key=contest, problem_index=index, **kw,
    )


class StoreTests(DbTestCase):
    def setUp(self):
        super().setUp()
        self.judge = FakeJudge()
        self.u = self.user("alice", cf="Alice_CF")

    def stored(self, user=None):
        return self.db.query(models.Submission).filter_by(user_id=(user or self.u).id, platform="fake").count()

    def state(self, user=None):
        return submissions.sync_state(self.db, (user or self.u).id, "fake")

    async def test_first_refresh_loads_everything_and_records_the_state(self):
        self.judge.rows = [sub(i) for i in range(1, 51)]
        state = await submissions.refresh_user(self.db, self.u, self.judge)
        self.assertEqual(self.stored(), 50)
        self.assertEqual((state.submission_count, state.newest_submission_id), (50, 50))
        self.assertEqual(state.newest_submitted_at, 1_600_000_050)
        self.assertEqual(state.handle, "Alice_CF")
        self.assertIsNotNone(state.last_synced_at)
        self.assertIsNotNone(state.full_sync_at)
        self.assertIsNone(state.last_error)
        self.assertEqual(self.judge.calls[0][1], KnownState())  # nothing known: full history requested
        self.assertTrue(submissions.is_synced(self.db, self.u.id, "fake"))

    async def test_a_recent_copy_is_not_refreshed_again(self):
        self.judge.rows = [sub(1)]
        await submissions.refresh_user(self.db, self.u, self.judge)
        await submissions.refresh_user(self.db, self.u, self.judge)
        self.assertEqual(len(self.judge.calls), 1)

    async def test_force_and_expiry_refresh_and_only_ask_for_what_is_newer(self):
        self.judge.rows = [sub(i) for i in range(1, 101)]
        first = await submissions.refresh_user(self.db, self.u, self.judge)
        full_sync_at = first.full_sync_at

        self.judge.rows += [sub(i) for i in range(101, 106)]
        state = await submissions.refresh_user(self.db, self.u, self.judge, force=True)
        self.assertEqual(self.judge.calls[1][1].stop_id, 100)  # asked to reach back only to the newest stored id
        self.assertEqual(self.stored(), 105)
        self.assertEqual(state.submission_count, 105)
        self.assertEqual(state.full_sync_at, full_sync_at)  # the full load happened once

        # an old copy is refreshed without being forced
        state.last_synced_at = datetime.utcnow() - submissions.REFRESH_MAX_AGE - timedelta(seconds=1)
        self.db.commit()
        self.judge.rows.append(sub(106))
        await submissions.refresh_user(self.db, self.u, self.judge)
        self.assertEqual(self.stored(), 106)
        self.assertEqual(len(self.judge.calls), 3)

    async def test_refreshing_twice_never_duplicates_rows(self):
        self.judge.rows = [sub(i) for i in range(1, 30)]
        for _ in range(3):
            await submissions.refresh_user(self.db, self.u, self.judge, force=True)
        self.assertEqual(self.stored(), 29)

    async def test_a_submission_still_being_judged_is_refetched_until_final(self):
        self.judge.rows = [sub(1), sub(2), sub(3, verdict="", final=False), sub(4)]
        await submissions.refresh_user(self.db, self.u, self.judge)
        # the pending one (id 3) is what the next refresh has to reach back to, not just the newest (4)
        self.judge.rows[2] = sub(3, verdict="AC", final=True)
        await submissions.refresh_user(self.db, self.u, self.judge, force=True)
        self.assertEqual(self.judge.calls[1][1].stop_id, 3)
        row = self.db.query(models.Submission).filter_by(user_id=self.u.id, submission_id=3).one()
        self.assertEqual((row.verdict, row.accepted, row.final), ("AC", True, True))
        self.assertEqual(self.stored(), 4)
        # nothing pending any more: the next refresh reaches back to the newest id only
        await submissions.refresh_user(self.db, self.u, self.judge, force=True)
        self.assertEqual(self.judge.calls[2][1].stop_id, 4)

    async def test_a_rejudged_verdict_is_updated_in_place(self):
        self.judge.rows = [sub(1, verdict="WA"), sub(2, verdict="AC")]
        await submissions.refresh_user(self.db, self.u, self.judge)
        self.judge.rows = [sub(1, verdict="AC"), sub(2, verdict="AC")]
        self.db.query(models.Submission).filter_by(submission_id=1).update({"final": False})
        self.db.commit()
        await submissions.refresh_user(self.db, self.u, self.judge, force=True)
        row = self.db.query(models.Submission).filter_by(user_id=self.u.id, submission_id=1).one()
        self.assertEqual((row.verdict, row.accepted), ("AC", True))

    async def test_duplicate_ids_in_one_batch_are_stored_once(self):
        self.judge.rows = [sub(1), sub(1), sub(2)]
        await submissions.refresh_user(self.db, self.u, self.judge)
        self.assertEqual(self.stored(), 2)

    async def test_failed_first_load_leaves_nothing_but_the_error(self):
        self.judge.fail_with = ValueError("CF API error: handle not found")
        with self.assertRaises(ValueError):
            await submissions.refresh_user(self.db, self.u, self.judge)
        self.assertEqual(self.stored(), 0)
        state = self.state()
        self.assertIn("handle not found", state.last_error)
        self.assertIsNone(state.last_synced_at)
        self.assertFalse(submissions.is_synced(self.db, self.u.id, "fake"))

    async def test_failed_refresh_keeps_the_stored_copy(self):
        self.judge.rows = [sub(i) for i in range(1, 11)]
        await submissions.refresh_user(self.db, self.u, self.judge)
        synced_at = self.state().last_synced_at
        self.judge.fail_with = ConnectionError("timeout")
        with self.assertRaises(ConnectionError):
            await submissions.refresh_user(self.db, self.u, self.judge, force=True)
        self.assertEqual(self.stored(), 10)
        state = self.state()
        self.assertEqual(state.last_synced_at, synced_at)
        self.assertEqual(state.last_error, "timeout")
        self.assertTrue(submissions.is_synced(self.db, self.u.id, "fake"))  # stale but usable
        # and it recovers
        self.judge.fail_with = None
        await submissions.refresh_user(self.db, self.u, self.judge, force=True)
        self.assertIsNone(self.state().last_error)

    async def test_changing_the_handle_resets_the_stored_copy(self):
        self.judge.rows = [sub(i) for i in range(1, 6)]
        await submissions.refresh_user(self.db, self.u, self.judge)
        self.db.add(models.RatingEntry(user_id=self.u.id, platform="fake", contest_key="1", new_rating=1500))
        self.db.commit()

        self.u.codeforces_handle = "someone_else"
        self.db.commit()
        self.judge.rows = [sub(100), sub(101)]
        await submissions.refresh_user(self.db, self.u, self.judge)
        ids = sorted(r.submission_id for r in self.db.query(models.Submission).filter_by(user_id=self.u.id))
        self.assertEqual(ids, [100, 101])  # the old handle's submissions are gone
        self.assertEqual(self.judge.calls[-1], ("someone_else", KnownState()))  # and it was a full load
        self.assertEqual(self.state().handle, "someone_else")
        self.assertEqual(self.db.query(models.RatingEntry).filter_by(user_id=self.u.id, platform="fake").count(), 0)

    async def test_a_handle_that_only_differs_in_case_is_the_same_handle(self):
        self.judge.rows = [sub(1), sub(2)]
        await submissions.refresh_user(self.db, self.u, self.judge)
        self.u.codeforces_handle = "alice_cf"
        self.db.commit()
        self.judge.rows.append(sub(3))
        await submissions.refresh_user(self.db, self.u, self.judge, force=True)
        self.assertEqual(self.stored(), 3)
        self.assertEqual(self.judge.calls[-1][1].stop_id, 2)  # incremental, not a reload
        self.assertEqual(self.state().handle, "alice_cf")

    async def test_concurrent_refreshes_of_one_user_fetch_once(self):
        self.judge.rows = [sub(i) for i in range(1, 20)]
        self.judge.delay = 0.05
        await asyncio.gather(*[submissions.refresh_user(SessionFactory(), self.u, self.judge) for _ in range(6)])
        self.assertEqual(len(self.judge.calls), 1)
        self.assertEqual(self.stored(), 19)

    async def test_users_are_independent(self):
        bob = self.user("bob", cf="Bob_CF")
        self.judge.rows = [sub(1), sub(2)]
        await submissions.refresh_user(self.db, self.u, self.judge)
        await submissions.refresh_user(self.db, bob, self.judge)
        # the same submission id (e.g. a team submission) is kept for each user
        self.assertEqual((self.stored(self.u), self.stored(bob)), (2, 2))

    async def test_refresh_users_reports_who_failed_and_still_refreshes_the_rest(self):
        bob = self.user("bob", cf="Bob_CF")
        carol = self.user("carol", cf="Carol_CF")
        self.judge.rows = [sub(1)]

        real = self.judge.fetch_submissions

        async def picky(handle, known):
            if handle == "Bob_CF":
                raise ValueError("no such user")
            return await real(handle, known)

        self.judge.fetch_submissions = picky
        errors = await submissions.refresh_users(self.db, [self.u, bob, carol], self.judge)
        self.assertEqual(list(errors), [bob.id])
        self.assertIn("no such user", errors[bob.id])
        self.assertEqual((self.stored(self.u), self.stored(bob), self.stored(carol)), (1, 0, 1))

    async def test_no_handle_or_no_submission_support_is_a_no_op(self):
        nobody = self.user("nobody")
        self.assertIsNone(await submissions.refresh_user(self.db, nobody, self.judge))
        self.assertEqual(self.judge.calls, [])
        self.assertIsNone(submissions.sync_state(self.db, nobody.id, "fake"))

        class Manual(FakeJudge):
            has_submissions = False
        self.assertIsNone(await submissions.refresh_user(self.db, self.u, Manual()))

    async def test_rating_history_is_replaced_on_each_refresh(self):
        self.judge.rows = [sub(1)]
        self.judge.history = [RatingData("10", "Round 10", 5, 1200, 1250), RatingData("11", "Round 11", 9, 1250, 1240),
                              RatingData("11", "duplicate", 1, 0, 0)]
        await submissions.refresh_user(self.db, self.u, self.judge)
        entries = {e.contest_key: e for e in self.db.query(models.RatingEntry).filter_by(user_id=self.u.id)}
        self.assertEqual(set(entries), {"10", "11"})
        self.assertEqual((entries["10"].old_rating, entries["10"].new_rating, entries["10"].rank), (1200, 1250, 5))
        self.assertEqual(submissions.rating_entry(self.db, self.u.id, "fake", "11").new_rating, 1240)

        self.judge.history = [RatingData("12", "Round 12", 3, 1240, 1300)]
        await submissions.refresh_user(self.db, self.u, self.judge, force=True)
        self.assertEqual([e.contest_key for e in self.db.query(models.RatingEntry).filter_by(user_id=self.u.id)], ["12"])

    async def test_platform_without_rating_history_leaves_entries_alone(self):
        self.judge.rows = [sub(1)]
        self.judge.history = None
        self.db.add(models.RatingEntry(user_id=self.u.id, platform="fake", contest_key="9", new_rating=1))
        self.db.commit()
        await submissions.refresh_user(self.db, self.u, self.judge)
        self.assertEqual(self.db.query(models.RatingEntry).filter_by(user_id=self.u.id).count(), 1)

    async def test_a_failing_rating_fetch_does_not_fail_the_refresh(self):
        self.judge.rows = [sub(1)]
        self.judge.history_fails = True
        state = await submissions.refresh_user(self.db, self.u, self.judge)
        self.assertEqual(state.submission_count, 1)
        self.assertIsNone(state.last_error)

    async def test_queries(self):
        self.judge.rows = [
            sub(5, "9/B", contest="9", index="B", at=300),
            sub(3, "9/A", contest="9", index="A", at=100),
            sub(4, "9/A", contest="9", index="A", at=200, verdict="WA"),
            sub(6, "7/A", contest="7", index="A", at=400),
            sub(7, "9/A", contest="9", index="A", at=200),  # same second as id 4
        ]
        await submissions.refresh_user(self.db, self.u, self.judge)
        u = self.u.id
        self.assertEqual([s.submission_id for s in submissions.for_contest(self.db, u, "fake", "9")], [3, 4, 7, 5])
        self.assertEqual([s.submission_id for s in submissions.for_problem(self.db, u, "fake", "9/A")], [3, 4, 7])
        self.assertEqual([s.submission_id for s in submissions.for_problems(self.db, u, "fake", ["9/B", "7/A"])], [5, 6])
        self.assertEqual(submissions.for_contest(self.db, u, "fake", "nope"), [])
        self.assertEqual(submissions.for_problem(self.db, u, "other", "9/A"), [])  # another platform's rows are separate

    async def test_for_problems_handles_thousands_of_keys(self):
        self.judge.rows = [sub(i, f"1/P{i}", index=f"P{i}") for i in range(1, 1201)]
        await submissions.refresh_user(self.db, self.u, self.judge)
        keys = [f"1/P{i}" for i in range(1, 1201)]
        self.assertEqual(len(submissions.for_problems(self.db, self.u.id, "fake", keys)), 1200)

    async def test_daily_counts(self):
        day = lambda d: int(datetime(2026, 5, d, 12).timestamp()) - time.timezone  # noqa: E731 (local -> ~UTC noon)
        self.judge.rows = [sub(1, at=day(1)), sub(2, at=day(1)), sub(3, at=day(2)), sub(4, at=1_000_000)]
        await submissions.refresh_user(self.db, self.u, self.judge)
        counts = submissions.daily_counts(self.db, self.u.id, "fake", since_epoch=day(1) - 3600)
        self.assertEqual(counts, {"2026-05-01": 2, "2026-05-02": 1})

    async def test_delete_user_data(self):
        bob = self.user("bob", cf="Bob_CF")
        self.judge.rows = [sub(1), sub(2)]
        self.judge.history = [RatingData("1", "R", 1, 0, 100)]
        await submissions.refresh_user(self.db, self.u, self.judge)
        await submissions.refresh_user(self.db, bob, self.judge)
        submissions.delete_user_data(self.db, self.u.id)
        self.db.commit()
        self.assertEqual(self.stored(self.u), 0)
        self.assertIsNone(self.state())
        self.assertEqual(self.db.query(models.RatingEntry).filter_by(user_id=self.u.id).count(), 0)
        self.assertEqual(self.stored(bob), 2)

    async def test_a_history_of_15000_submissions_loads_and_refreshes_quickly(self):
        self.judge.rows = [sub(i, f"{i % 900}/A", contest=str(i % 900)) for i in range(1, 15001)]
        t = time.time()
        await submissions.refresh_user(self.db, self.u, self.judge)
        load = time.time() - t
        self.assertEqual(self.stored(), 15000)
        self.judge.rows += [sub(i) for i in range(15001, 15006)]
        t = time.time()
        await submissions.refresh_user(self.db, self.u, self.judge, force=True)
        refresh = time.time() - t
        self.assertEqual(self.stored(), 15005)
        self.assertLess(load, 15, f"initial load took {load:.1f}s")
        self.assertLess(refresh, 2, f"incremental refresh took {refresh:.1f}s")
        t = time.time()
        subs = submissions.for_contest(self.db, self.u.id, "fake", "7")
        self.assertGreater(len(subs), 0)
        self.assertLess(time.time() - t, 0.5)


def SessionFactory():
    from app.database import SessionLocal
    return SessionLocal()
