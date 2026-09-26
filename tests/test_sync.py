"""Item sync end to end: submissions in, Result / ProblemResult rows out, with the judges' APIs faked."""
from datetime import datetime, timedelta
from unittest import mock

from app import models, submissions, sync
from app.scraper import atcoder as ac_api
from app.scraper import codeforces as cf_api
from app.scraper import kilonova as kn_api
from tests.support import DbTestCase, ac_raw, cf_raw, kn_raw


class FakeCodeforces:
    """Patches the Codeforces API module. `status[handle]` is a user's history, newest first."""

    def __init__(self, testcase):
        self.status: dict[str, list] = {}
        self.ratings: dict[str, list] = {}
        self.contests: dict[str, tuple[str, list]] = {}      # id -> (title, problems)
        self.gyms: dict[str, dict] = {}                      # id -> {"name", "phase", "problems"}
        self.problem_ratings: dict[tuple[str, str], int] = {}
        self.fail_status: set[str] = set()
        self.calls: list[str] = []
        self.not_started: set[str] = set()
        self.info_error: Exception | None = None

        async def get_user_status(handle, offset=1, count=100000):
            self.calls.append(f"status:{handle}")
            if handle in self.fail_status:
                raise ValueError("CF API error: handle: User with handle %s not found" % handle)
            return self.status.get(handle, [])[offset - 1: offset - 1 + count]

        async def get_user_rating_history(handle):
            self.calls.append(f"rating:{handle}")
            return self.ratings.get(handle, [])

        async def get_contest_info(contest_id):
            self.calls.append(f"info:{contest_id}")
            if self.info_error:
                raise self.info_error
            if contest_id in self.not_started:
                raise cf_api.ContestNotStartedError(contest_id)
            title, problems = self.contests[contest_id]
            return {"title": title, "problems": problems}

        async def get_contest_problems(contest_id):
            self.calls.append(f"problems:{contest_id}")
            return self.contests[contest_id][1]

        async def get_contest_metadata(contest_id):
            self.calls.append(f"meta:{contest_id}")
            return {"id": int(contest_id), "name": f"Contest {contest_id} (upcoming)"}

        async def get_problem_rating(contest_id, index):
            self.calls.append(f"rating-of:{contest_id}{index}")
            return self.problem_ratings.get((contest_id, index))

        async def get_gym_metadata(contest_id):
            self.calls.append(f"gym-meta:{contest_id}")
            g = self.gyms.get(contest_id)
            return {"id": int(contest_id), "name": g["name"], "phase": g.get("phase", "FINISHED")} if g else None

        async def get_gym_problems(contest_id):
            self.calls.append(f"gym-problems:{contest_id}")
            return self.gyms[contest_id]["problems"]

        for name, fn in [("get_user_status", get_user_status), ("get_user_rating_history", get_user_rating_history),
                         ("get_contest_info", get_contest_info), ("get_contest_problems", get_contest_problems),
                         ("get_contest_metadata", get_contest_metadata), ("get_problem_rating", get_problem_rating),
                         ("get_gym_metadata", get_gym_metadata), ("get_gym_problems", get_gym_problems)]:
            p = mock.patch.object(cf_api, name, fn)
            p.start()
            testcase.addCleanup(p.stop)

    def count(self, prefix):
        return sum(1 for c in self.calls if c.startswith(prefix))


class SyncTestCase(DbTestCase):
    async def sync(self, item):
        await sync.sync_item(item.id, self.db)
        self.db.refresh(item)
        return item

    def result(self, item, user):
        self.db.expire_all()
        return self.db.query(models.Result).filter_by(assignment_item_id=item.id, user_id=user.id).first()

    def problem_results(self, item, user):
        self.db.expire_all()
        rows = (
            self.db.query(models.ProblemResult, models.ContestProblem)
            .join(models.ContestProblem)
            .filter(models.ContestProblem.assignment_item_id == item.id, models.ProblemResult.user_id == user.id)
            .all()
        )
        return {cp.index: pr for pr, cp in rows}

    def age_stores(self):
        """Pretend the stored submissions were last refreshed a while ago."""
        old = datetime.utcnow() - timedelta(hours=1)
        self.db.query(models.SubmissionSync).update({"last_synced_at": old})
        self.db.commit()


class CodeforcesContestSync(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.cf = FakeCodeforces(self)
        self.cf.contests["2000"] = ("Round 2000", [
            {"index": "A", "name": "Alpha", "rating": 800}, {"index": "B", "name": "Beta", "rating": 1200},
            {"index": "C", "name": "Gamma"}, {"index": "D", "name": "Delta", "rating": 2000},
        ])
        self.alice = self.user("alice", cf="alice_cf")
        self.bob = self.user("bob", cf="bob_cf")
        _, self.assignment = self.group([self.alice, self.bob])
        self.item_ = self.item(self.assignment, "codeforces", "contest", "2000")
        self.cf.status["alice_cf"] = [
            cf_raw(9, 2000, "D", "WRONG_ANSWER", at=9000, ptype="PRACTICE"),
            cf_raw(8, 2000, "C", "OK", at=8000, ptype="PRACTICE"),                       # upsolved
            cf_raw(4, 2000, "B", "OK", at=4000, ptype="CONTESTANT", rel=3000),
            cf_raw(3, 2000, "B", "WRONG_ANSWER", at=3500, ptype="CONTESTANT"),
            cf_raw(2, 2000, "A", "OK", at=2000, ptype="CONTESTANT", rel=1000),
            cf_raw(1, 1, "A", "OK", at=1, ptype="PRACTICE"),                              # unrelated contest
        ]
        self.cf.ratings["alice_cf"] = [{"contestId": 2000, "contestName": "Round 2000", "rank": 321,
                                        "ratingUpdateTimeSeconds": 10, "oldRating": 1500, "newRating": 1442}]

    async def test_results_are_derived_from_the_stored_submissions(self):
        item = await self.sync(self.item_)
        self.assertEqual((item.sync_status, item.sync_error), ("done", None))
        self.assertEqual(item.title, "Round 2000")
        self.assertEqual([(p.index, p.name, p.rating) for p in sorted(item.contest_problems, key=lambda p: p.index)],
                         [("A", "Alpha", 800), ("B", "Beta", 1200), ("C", "Gamma", None), ("D", "Delta", 2000)])

        r = self.result(item, self.alice)
        self.assertEqual((r.problems_solved_count, r.problems_total_count, r.participated), (3, 4, True))
        self.assertEqual((r.old_rating, r.new_rating, r.rating_change), (1500, 1442, -58))
        pr = self.problem_results(item, self.alice)
        self.assertEqual((pr["A"].solved, pr["A"].solve_type, pr["A"].attempts), (True, "live", None))
        self.assertEqual((pr["B"].solved, pr["B"].solve_type, pr["B"].attempts, pr["B"].best_wrong_verdict), (True, "live", 1, "WA"))
        self.assertEqual((pr["C"].solved, pr["C"].solve_type), (True, "upsolving"))
        self.assertEqual((pr["D"].solved, pr["D"].solve_type, pr["D"].attempts, pr["D"].best_wrong_verdict), (False, None, 1, "WA"))

    async def test_a_member_with_no_submissions_gets_empty_results_not_missing_ones(self):
        item = await self.sync(self.item_)
        r = self.result(item, self.bob)
        self.assertEqual((r.problems_solved_count, r.participated), (0, False))
        pr = self.problem_results(item, self.bob)
        self.assertEqual(set(pr), {"A", "B", "C", "D"})
        self.assertFalse(any(p.solved for p in pr.values()))

    async def test_the_contest_is_not_looked_up_again_and_the_store_is_reused(self):
        await self.sync(self.item_)
        self.assertEqual((self.cf.count("info:"), self.cf.count("status:alice_cf")), (1, 1))
        await self.sync(self.item_)
        self.assertEqual((self.cf.count("info:"), self.cf.count("status:alice_cf")), (1, 1))  # fresh copy, known contest

    async def test_russian_names_from_before_english_was_requested_are_replaced_on_the_next_sync(self):
        await self.sync(self.item_)
        self.db.refresh(self.item_)
        self.item_.title = "Раунд 2000"
        self.item_.contest_problems[0].name = "Альфа"
        self.db.commit()
        await self.sync(self.item_)
        self.db.expire_all()
        item = self.db.get(models.AssignmentItem, self.item_.id)
        self.assertEqual(item.title, "Round 2000")
        self.assertEqual(sorted(cp.name for cp in item.contest_problems), ["Alpha", "Beta", "Delta", "Gamma"])
        infos = self.cf.count("info:")
        await self.sync(self.item_)
        self.assertEqual(self.cf.count("info:"), infos)               # English names: not looked up again

    async def test_a_given_title_in_russian_is_kept_for_a_gym(self):
        gym = self.item(self.assignment, "codeforces", "contest", "100500", title="Тренировка")
        self.cf.gyms["100500"] = {"name": "Тренировка", "phase": "FINISHED", "problems": [{"index": "A", "name": "Задача"}]}
        await self.sync(gym)
        self.db.expire_all()
        self.assertEqual(self.db.get(models.AssignmentItem, gym.id).title, "Тренировка")
        await self.sync(gym)
        self.assertEqual(self.cf.count("gym-problems"), 1)            # a gym's Russian names are its own: no re-fetching

    async def test_a_new_submission_is_picked_up_by_the_next_refresh_incrementally(self):
        await self.sync(self.item_)
        self.cf.status["alice_cf"].insert(0, cf_raw(20, 2000, "D", "OK", at=20000, ptype="PRACTICE"))
        self.age_stores()
        await self.sync(self.item_)
        pr = self.problem_results(self.item_, self.alice)
        self.assertEqual((pr["D"].solved, pr["D"].solve_type, pr["D"].attempts), (True, "upsolving", 1))
        self.assertEqual(self.result(self.item_, self.alice).problems_solved_count, 4)
        # the second call asked only for the newest page, and the stored history was kept whole
        stored = self.db.query(models.Submission).filter_by(user_id=self.alice.id).count()
        self.assertEqual(stored, 7)

    async def test_syncing_repeatedly_does_not_duplicate_anything(self):
        for _ in range(3):
            self.age_stores()
            await self.sync(self.item_)
        self.assertEqual(self.db.query(models.Result).filter_by(assignment_item_id=self.item_.id).count(), 2)
        self.assertEqual(self.db.query(models.ContestProblem).filter_by(assignment_item_id=self.item_.id).count(), 4)
        self.assertEqual(self.db.query(models.ProblemResult).count(), 8)

    async def test_a_member_without_a_handle_is_left_out(self):
        carol = self.user("carol")
        self.db.add(models.GroupMembership(group_id=self.assignment.group_id, user_id=carol.id))
        self.db.commit()
        await self.sync(self.item_)
        self.assertIsNone(self.result(self.item_, carol))
        self.assertNotIn("status:None", self.cf.calls)

    async def test_a_failing_member_does_not_block_the_others_and_the_error_names_them(self):
        self.cf.fail_status.add("bob_cf")
        item = await self.sync(self.item_)
        self.assertEqual(item.sync_status, "error")
        self.assertIn("bob_cf", item.sync_error)
        self.assertIn("Showing stored data", item.sync_error)
        self.assertEqual(self.result(item, self.alice).problems_solved_count, 3)  # alice is still up to date
        self.assertIsNone(self.result(item, self.bob))                             # bob is unknown, not "solved nothing"

    async def test_a_member_whose_earlier_copy_exists_keeps_showing_it_when_the_judge_is_down(self):
        await self.sync(self.item_)
        self.cf.fail_status.add("alice_cf")
        self.age_stores()
        item = await self.sync(self.item_)
        self.assertEqual(item.sync_status, "error")
        self.assertEqual(self.result(item, self.alice).problems_solved_count, 3)  # stale but not lost

    async def test_contest_that_has_not_started(self):
        self.cf.not_started.add("2000")
        item = await self.sync(self.item_)
        self.assertEqual((item.sync_status, item.sync_error), ("not_started", "Contest has not started yet"))
        self.assertEqual(item.title, "Contest 2000 (upcoming)")

    async def test_problems_are_taken_from_submissions_when_the_listing_fails(self):
        self.cf.info_error = ValueError("CF API error: something odd")
        item = await self.sync(self.item_)
        self.assertEqual(item.sync_status, "done")
        self.assertEqual(item.title, "Contest 2000")
        self.assertEqual(sorted(p.index for p in item.contest_problems), ["A", "B", "C", "D"])
        self.assertEqual(self.result(item, self.alice).problems_solved_count, 3)

    async def test_missing_problem_ratings_are_looked_up_again_only_after_a_while(self):
        item = await self.sync(self.item_)                       # problem C has no rating yet
        item.last_synced_at = datetime.utcnow() - timedelta(hours=1)
        self.db.commit()
        await self.sync(item)
        self.assertEqual(self.cf.count("info:"), 1)              # too soon to ask again
        item.last_synced_at = datetime.utcnow() - timedelta(hours=7)
        self.db.commit()
        self.cf.contests["2000"][1][2]["rating"] = 1600          # the rating has been published
        await self.sync(item)
        self.assertEqual(self.cf.count("info:"), 2)
        self.assertEqual({p.index: p.rating for p in item.contest_problems}["C"], 1600)


class CodeforcesGymSync(SyncTestCase):
    """The case from the request: a three-person team's virtual participation in a gym."""

    TEAM = (226957, "[UAIC] paul_diac_goat")

    def setUp(self):
        super().setUp()
        self.cf = FakeCodeforces(self)
        self.cf.gyms["105427"] = {"name": "2023-2024 ICPC NCPC 2023", "problems": [
            {"index": i, "name": f"Problem {i}"} for i in "ABCDEFGHIJK"]}
        self.members = [self.user(n, cf=h) for n, h in (("a", "alg0rel"), ("h", "harmito"), ("c", "cristi_tanase"))]
        _, self.assignment = self.group(self.members)
        # every team member's user.status contains the team's submissions
        team = [
            cf_raw(120, 105427, "K", "OK", at=5000, ptype="VIRTUAL", team=self.TEAM),
            cf_raw(110, 105427, "E", "TIME_LIMIT_EXCEEDED", at=4000, ptype="VIRTUAL", team=self.TEAM),
            cf_raw(105, 105427, "A", "OK", at=1000, ptype="VIRTUAL", team=self.TEAM),
            cf_raw(102, 105427, "A", "WRONG_ANSWER", at=900, ptype="VIRTUAL", team=self.TEAM),
        ]
        for handle in ("alg0rel", "harmito", "cristi_tanase"):
            self.cf.status[handle] = list(team)

    async def test_team_participation_is_recorded_for_every_member(self):
        item = await self.sync(self.item(self.assignment, "codeforces", "contest", "105427"))
        self.assertEqual((item.sync_status, item.title), ("done", "2023-2024 ICPC NCPC 2023"))
        self.assertEqual(len(item.contest_problems), 11)
        for member in self.members:
            r = self.result(item, member)
            self.assertEqual((r.problems_solved_count, r.problems_total_count, r.participated), (2, 11, True))
            pr = self.problem_results(item, member)
            self.assertEqual((pr["A"].solve_type, pr["A"].attempts, pr["A"].best_wrong_verdict), ("virtual", 1, "WA"))
            self.assertEqual(pr["K"].solve_type, "virtual")
            self.assertEqual((pr["E"].solved, pr["E"].best_wrong_verdict), (False, "TLE"))
        # the gym's problems came from the gym endpoints, never from standings
        self.assertEqual(self.cf.count("info:"), 0)
        self.assertEqual(self.cf.count("gym-problems:"), 1)

    async def test_gym_that_has_not_started(self):
        self.cf.gyms["105427"]["phase"] = "BEFORE"
        item = await self.sync(self.item(self.assignment, "codeforces", "contest", "105427"))
        self.assertEqual(item.sync_status, "not_started")

    async def test_gym_problem_as_a_standalone_item(self):
        item = await self.sync(self.item(self.assignment, "codeforces", "problem", "105427/A"))
        self.assertEqual(item.title, "PA")  # the name on the submissions (cf_raw names problem X "PX")
        self.assertIsNone(item.rating)
        self.assertEqual(self.cf.count("rating-of:"), 0)  # gyms have no ratings, so nothing to look up
        for member in self.members:
            self.assertTrue(self.result(item, member).solved)
        unsolved = await self.sync(self.item(self.assignment, "codeforces", "problem", "105427/B"))
        self.assertEqual(unsolved.title, "Problem B")  # nobody submitted it: the title comes from the gym's problem list
        for member in self.members:
            self.assertFalse(self.result(unsolved, member).solved)


class CodeforcesProblemSync(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.cf = FakeCodeforces(self)
        self.cf.contests["1"] = ("Beta Round 1", [{"index": "A", "name": "Theatre Square", "rating": 1000}])
        self.cf.problem_ratings[("1", "A")] = 1000
        self.alice = self.user("alice", cf="alice_cf")
        self.bob = self.user("bob", cf="bob_cf")
        self.carol = self.user("carol")  # no handle
        _, self.assignment = self.group([self.alice, self.bob, self.carol])
        name = "Theatre Square"
        self.cf.status["alice_cf"] = [cf_raw(2, 1, "A", "OK", at=200, name=name), cf_raw(1, 1, "A", "WRONG_ANSWER", at=100, name=name)]
        self.cf.status["bob_cf"] = [cf_raw(3, 1, "A", "WRONG_ANSWER", at=300, name=name)]

    async def test_solved_unsolved_and_unknown(self):
        item = await self.sync(self.item(self.assignment, "codeforces", "problem", "1/A"))
        self.assertEqual((item.sync_status, item.title, item.rating), ("done", "Theatre Square", 1000))
        self.assertTrue(self.result(item, self.alice).solved)
        self.assertEqual(self.result(item, self.alice).solve_time, datetime.utcfromtimestamp(200))
        self.assertFalse(self.result(item, self.bob).solved)
        self.assertIsNone(self.result(item, self.carol))  # no handle: unknown, not "unsolved"

    async def test_a_given_title_is_kept(self):
        item = await self.sync(self.item(self.assignment, "codeforces", "problem", "1/A", title="My name for it"))
        self.assertEqual(item.title, "My name for it")

    async def test_edu_problems_are_never_looked_up_or_marked(self):
        edu = self.item(self.assignment, "codeforces", "problem", "274545/A",
                        source_url="https://codeforces.com/edu/course/2/lesson/4/3/practice/contest/274545/problem/A")
        item = await self.sync(edu)
        self.assertEqual(item.sync_status, "done")
        self.assertEqual(self.cf.calls, [])                  # not even a submissions refresh
        self.assertEqual(self.db.query(models.Result).count(), 0)

    async def test_solving_after_the_first_sync_shows_up_on_the_next(self):
        item = await self.sync(self.item(self.assignment, "codeforces", "problem", "1/A"))
        self.cf.status["bob_cf"].insert(0, cf_raw(9, 1, "A", "OK", at=900))
        self.age_stores()
        await self.sync(item)
        self.assertTrue(self.result(item, self.bob).solved)


class AtCoderSync(SyncTestCase):
    START, DURATION = 1_700_000_000, 6000

    def setUp(self):
        super().setUp()
        self.subs = {}
        self.calls = []

        async def get_user_submissions(handle, from_epoch=0):
            self.calls.append(("subs", handle, from_epoch))
            return [s for s in self.subs.get(handle, []) if s["epoch_second"] >= from_epoch]

        async def get_rating_history(handle):
            return [{"IsRated": True, "Place": 12, "OldRating": 1000, "NewRating": 1100, "Performance": 1500,
                     "ContestScreenName": "abc300.contest.atcoder.jp", "ContestName": "ABC 300",
                     "EndTime": "2023-11-14T22:40:00+09:00"}] if handle == "rated_user" else []

        async def get_contest_tasks(contest_id):
            return [
                {"id": "abc300_a", "title": "A. Fifth", "difficulty": 100, "contest_index": "A"},
                {"id": "abc300_b", "title": "B. Second", "difficulty": 300, "contest_index": "B"},
                {"id": "abc200_z", "title": "Z. Reused", "difficulty": None, "contest_index": "C"},
            ]

        async def get_contest_timing(contest_id):
            return {"start_epoch_second": self.START, "duration_second": self.DURATION}

        async def get_problem_title(problem_id):
            return "A - Fifth"

        for name, fn in [("get_user_submissions", get_user_submissions), ("get_rating_history", get_rating_history),
                         ("get_contest_tasks", get_contest_tasks), ("get_contest_timing", get_contest_timing),
                         ("get_problem_title", get_problem_title)]:
            p = mock.patch.object(ac_api, name, fn)
            p.start()
            self.addCleanup(p.stop)

        self.rated = self.user("rated", ac="rated_user")
        self.casual = self.user("casual", ac="casual_user")
        self.nohandle = self.user("nohandle")
        _, self.assignment = self.group([self.rated, self.casual, self.nohandle])
        S = self.START
        self.subs["rated_user"] = [
            ac_raw(1, "abc300_a", "WA", at=S + 100), ac_raw(2, "abc300_a", "AC", at=S + 200),
            ac_raw(3, "abc300_b", "AC", at=S + self.DURATION + 500),
        ]
        self.subs["casual_user"] = [
            ac_raw(10, "abc300_b", "AC", at=S + self.DURATION + 900),
            ac_raw(11, "abc200_z", "AC", at=S - 90_000),
        ]

    async def test_contest(self):
        item = await self.sync(self.item(self.assignment, "atcoder", "contest", "abc300"))
        self.assertEqual((item.sync_status, item.title), ("done", "AtCoder Beginner Contest 300"))
        self.assertEqual([(p.index, p.name, p.platform_problem_id) for p in sorted(item.contest_problems, key=lambda p: p.index)],
                         [("A", "Fifth", "abc300_a"), ("B", "Second", "abc300_b"), ("C", "Reused", "abc200_z")])

        r = self.result(item, self.rated)
        self.assertEqual((r.problems_solved_count, r.problems_total_count, r.participated), (2, 3, True))
        self.assertEqual((r.rank, r.old_rating, r.new_rating, r.rating_change), (12, 1000, 1100, 100))
        pr = self.problem_results(item, self.rated)
        self.assertEqual((pr["A"].solve_type, pr["A"].attempts, pr["A"].best_wrong_verdict), ("live", 1, "WA"))
        self.assertEqual(pr["B"].solve_type, "upsolving")   # rated in the contest, so a later solve is an upsolve
        self.assertFalse(pr["C"].solved)

        r = self.result(item, self.casual)
        self.assertEqual((r.problems_solved_count, r.participated, r.rank), (2, False, None))
        pr = self.problem_results(item, self.casual)
        self.assertEqual(pr["B"].solve_type, "standalone")  # never took part
        self.assertEqual(pr["C"].solve_type, "standalone")  # solved long before this contest reused it

        self.assertIsNone(self.result(item, self.nohandle))

    async def test_contest_without_timing_information(self):
        async def no_timing(contest_id):
            return None
        with mock.patch.object(ac_api, "get_contest_timing", no_timing):
            item = await self.sync(self.item(self.assignment, "atcoder", "contest", "abc300"))
        pr = self.problem_results(item, self.rated)
        self.assertEqual((pr["A"].solved, pr["A"].solve_type, pr["B"].solved), (True, None, True))

    async def test_standalone_problem(self):
        item = await self.sync(self.item(self.assignment, "atcoder", "problem", "abc300_a"))
        self.assertEqual(item.title, "A - Fifth")
        self.assertTrue(self.result(item, self.rated).solved)
        self.assertFalse(self.result(item, self.casual).solved)
        self.assertIsNone(self.result(item, self.nohandle))

    async def test_the_second_sync_only_asks_for_newer_submissions(self):
        item = await self.sync(self.item(self.assignment, "atcoder", "problem", "abc300_a"))
        self.age_stores()
        await self.sync(item)
        rated_calls = [c for c in self.calls if c[1] == "rated_user"]
        self.assertEqual(rated_calls[0][2], 0)
        self.assertEqual(rated_calls[1][2], self.START + self.DURATION + 500)  # from the newest stored submission


class KilonovaSync(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.subs = {5: [], 6: []}
        self.calls = []
        self.scales = {100: 100, 101: 100, 102: 50}

        async def get_user_id(name):
            return {"andrei": 5, "bianca": 6}.get(name)

        async def get_user_submissions(user_id, offset=0):
            rows = sorted(self.subs[user_id], key=lambda s: -s["id"])
            return rows[offset: offset + 50], len(rows)

        async def get_problem(pid):
            self.calls.append(("problem", pid))
            return {"id": pid, "name": f"Problem {pid}", "score_scale": self.scales[pid]}

        async def get_problem_list(list_id):
            self.calls.append(("list", list_id))
            return {"id": list_id, "title": "Warm-up list", "list": [100, 101, 102]}

        for name, fn in [("get_user_id", get_user_id), ("get_user_submissions", get_user_submissions),
                         ("get_problem", get_problem), ("get_problem_list", get_problem_list)]:
            p = mock.patch.object(kn_api, name, fn)
            p.start()
            self.addCleanup(p.stop)

        self.andrei = self.user("andrei_u", kn="andrei")
        self.bianca = self.user("bianca_u", kn="bianca")
        self.ghost = self.user("ghost_u", kn="ghost")
        _, self.assignment = self.group([self.andrei, self.bianca])
        self.subs[5] = [kn_raw(1, 100, 100), kn_raw(2, 101, 40), kn_raw(3, 101, 70), kn_raw(4, 102, 50, scale=50)]
        self.subs[6] = [kn_raw(5, 100, 0)]

    async def test_problem_list(self):
        item = await self.sync(self.item(self.assignment, "kilonova", "contest", "77"))
        self.assertEqual((item.sync_status, item.title), ("done", "Warm-up list"))
        self.assertEqual({p.platform_problem_id: p.max_score for p in item.contest_problems}, {"100": 100, "101": 100, "102": 50})

        r = self.result(item, self.andrei)
        self.assertEqual((r.problems_solved_count, r.problems_total_count), (2, 3))
        self.assertEqual(r.raw_scrape_data, {"kn_score": 100 + 70 + 50, "kn_total": 250})
        pr = self.problem_results(item, self.andrei)
        self.assertEqual([(pr[i].score, pr[i].solved) for i in "123"], [(100, True), (70, False), (50, True)])

        r = self.result(item, self.bianca)
        self.assertEqual((r.problems_solved_count, r.raw_scrape_data["kn_score"]), (0, 0))

    async def test_problem_infos_are_fetched_once_per_problem(self):
        item = await self.sync(self.item(self.assignment, "kilonova", "contest", "77"))
        await self.sync(item)
        self.assertEqual(sum(1 for c in self.calls if c[0] == "problem"), 3)
        self.assertEqual(sum(1 for c in self.calls if c[0] == "list"), 2)  # the list itself is checked every time

    async def test_standalone_problem(self):
        item = await self.sync(self.item(self.assignment, "kilonova", "problem", "101"))
        self.assertEqual(item.title, "Problem 101")
        r = self.result(item, self.andrei)
        self.assertEqual((r.solved, r.raw_scrape_data), (False, {"kn_score": 70, "kn_total": 100}))
        item2 = await self.sync(self.item(self.assignment, "kilonova", "problem", "102"))
        r = self.result(item2, self.andrei)
        self.assertEqual((r.solved, r.raw_scrape_data), (True, {"kn_score": 50, "kn_total": 50}))  # scale 50: 50 is full marks
        self.assertEqual(self.result(item2, self.bianca).raw_scrape_data, {"kn_score": 0, "kn_total": 50})

    async def test_unknown_handle_is_reported(self):
        self.db.add(models.GroupMembership(group_id=self.assignment.group_id, user_id=self.ghost.id))
        self.db.commit()
        item = await self.sync(self.item(self.assignment, "kilonova", "problem", "101"))
        self.assertEqual(item.sync_status, "error")
        self.assertIn("ghost", item.sync_error)
        self.assertEqual(self.result(item, self.andrei).raw_scrape_data["kn_score"], 70)  # the others are fine


class ManualPlatforms(SyncTestCase):
    async def test_cses_and_other_links_are_never_fetched_and_keep_their_marks(self):
        with mock.patch.object(cf_api, "get_user_status", side_effect=AssertionError("no network")):
            alice = self.user("alice", cf="a")
            _, assignment = self.group([alice])
            cses = self.item(assignment, "cses", "problem", "1068")
            other = self.item(assignment, "other", "problem", "url:abc", source_url="https://example.com/p/1")
            self.db.add(models.Result(assignment_item_id=cses.id, user_id=alice.id, solved=True))
            self.db.commit()
            for item in (cses, other):
                await self.sync(item)
                self.assertEqual((item.sync_status, item.sync_error), ("done", None))
        self.assertTrue(self.result(cses, alice).solved)  # the hand-made mark survives a sync
        self.assertIsNone(self.result(other, alice))
        self.assertEqual(cses.display_title, "CSES 1068")
        self.assertEqual(other.display_title, "https://example.com/p/1")
        self.assertTrue(cses.manual_status and other.manual_status)


class SyncEngine(SyncTestCase):
    async def test_unknown_item_id_is_ignored(self):
        await sync.sync_item(12345, self.db)

    async def test_unexpected_errors_are_recorded_on_the_item(self):
        cf = FakeCodeforces(self)
        u = self.user("alice", cf="alice_cf")
        _, assignment = self.group([u])
        item = self.item(assignment, "codeforces", "problem", "1/A")

        async def broken(*a, **k):
            raise RuntimeError("kaboom")
        with mock.patch("app.platforms.cf._sync_problem", broken):
            await self.sync(item)
        self.assertEqual((item.sync_status, item.sync_error), ("error", "kaboom"))

    async def test_timeout(self):
        u = self.user("alice", cf="alice_cf")
        _, assignment = self.group([u])
        item = self.item(assignment, "codeforces", "problem", "1/A")

        async def slow(*a, **k):
            import asyncio
            await asyncio.sleep(5)
        with mock.patch("app.sync.SYNC_TIMEOUT_SECONDS", 0.05), mock.patch("app.platforms.cf._sync_problem", slow):
            await self.sync(item)
        self.assertEqual(item.sync_status, "error")
        self.assertIn("timed out", item.sync_error)

    async def test_two_items_syncing_at_once_share_one_refresh_per_user(self):
        import asyncio
        cf = FakeCodeforces(self)
        cf.status["alice_cf"] = [cf_raw(1, 1, "A", "OK"), cf_raw(2, 2, "A", "OK")]
        cf.contests["1"] = ("R1", [{"index": "A", "name": "A1"}])
        cf.contests["2"] = ("R2", [{"index": "A", "name": "A2"}])
        u = self.user("alice", cf="alice_cf")
        _, assignment = self.group([u])
        items = [self.item(assignment, "codeforces", "problem", "1/A"), self.item(assignment, "codeforces", "problem", "2/A")]
        from app.database import SessionLocal
        await asyncio.gather(*[sync.sync_item(i.id, SessionLocal()) for i in items])
        self.assertEqual(cf.count("status:alice_cf"), 1)
        for it in items:
            self.db.refresh(it)
            self.assertEqual(it.sync_status, "done")
            self.assertTrue(self.result(it, u).solved)
