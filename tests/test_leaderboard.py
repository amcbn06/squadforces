"""The 30-day leaderboards: new problems only, first acceptance only, from the submission store."""
import time
from datetime import datetime

from app import leaderboard, models
from tests.support import DbTestCase

DAY = 86400


class Leaderboards(DbTestCase):
    def setUp(self):
        super().setUp()
        self.now = int(time.time())
        self.n = 0

    def sub(self, user, problem, days_ago, *, ok=True, platform="codeforces", contest=None):
        self.n += 1
        self.db.add(models.Submission(
            user_id=user.id, platform=platform, submission_id=self.n, problem_key=problem, contest_key=contest,
            verdict="AC" if ok else "WA", accepted=ok, submitted_at=self.now - int(days_ago * DAY)))
        self.db.commit()

    def loaded(self, *users, platform="codeforces"):
        for u in users:
            self.db.add(models.SubmissionSync(user_id=u.id, platform=platform, last_synced_at=datetime.utcnow(),
                                              full_sync_at=datetime.utcnow()))
        self.db.commit()

    def solved(self, rows):
        return {r.user.username: r.solved for r in rows}

    def test_only_the_first_acceptance_counts_and_only_inside_the_window(self):
        a = self.user("a")
        self.loaded(a)
        self.sub(a, "1/A", 5)                # new, counts
        self.sub(a, "1/A", 2)                # solved again later: still one
        self.sub(a, "1/B", 40)               # solved before the window
        self.sub(a, "1/B", 3)                # ...and again inside it: not new
        self.sub(a, "1/C", 4, ok=False)      # a wrong answer solves nothing
        self.sub(a, "1/D", 29)               # just inside
        self.sub(a, "1/E", 31)               # just outside
        self.assertEqual(leaderboard.new_solves(self.db, [a.id])[a.id], {("codeforces", "1/A"), ("codeforces", "1/D")})

    def test_a_problem_wrong_before_and_right_inside_the_window_is_new(self):
        a = self.user("a")
        self.loaded(a)
        self.sub(a, "2/A", 60, ok=False)
        self.sub(a, "2/A", 1)
        self.assertEqual(len(leaderboard.new_solves(self.db, [a.id])[a.id]), 1)

    def test_the_same_key_on_two_judges_is_two_problems_and_two_users_are_separate(self):
        a, b = self.user("a"), self.user("b")
        self.loaded(a, b)
        self.loaded(a, platform="kilonova")
        self.sub(a, "1", 1, platform="kilonova")
        self.sub(a, "1", 1)
        self.sub(b, "1", 50)                 # b solved it long ago, a solves it now
        self.sub(b, "1", 1)
        out = leaderboard.new_solves(self.db, [a.id, b.id])
        self.assertEqual(len(out[a.id]), 2)
        self.assertNotIn(b.id, out)

    def test_a_history_that_is_not_fully_loaded_yet_counts_for_nothing(self):
        a = self.user("a")                   # no full copy: an old acceptance might be missing
        self.sub(a, "1/A", 1)
        self.assertEqual(leaderboard.new_solves(self.db, [a.id]), {})

    def test_group_board_counts_only_the_groups_problems(self):
        a, b, c = self.user("a"), self.user("b"), self.user("c")
        self.loaded(a, b, c)
        g, asg = self.group([a, b, c])
        self.item(asg, "codeforces", "problem", "10/A")
        contest = self.item(asg, "codeforces", "contest", "20")
        for i in ("A", "B"):
            self.db.add(models.ContestProblem(assignment_item_id=contest.id, platform_problem_id=f"20{i}", index=i, name=i))
        self.db.commit()
        for p in ("10/A", "20/A", "20/B", "99/A"):     # a solves the group's three problems and one other
            self.sub(a, p, 3)
        self.sub(b, "20/A", 3)
        self.sub(b, "20/A", 1)                          # twice, counted once
        self.sub(c, "10/A", 45)                         # c solved it before the window
        self.sub(c, "10/A", 2)
        self.db.refresh(g)
        rows = leaderboard.for_group(self.db, g)
        self.assertEqual(self.solved(rows), {"a": 3, "b": 1, "c": 0})
        self.assertEqual([(r.user.username, r.rank) for r in rows], [("a", 1), ("b", 2), ("c", 3)])

    def test_atcoder_and_kilonova_items_use_their_own_problem_keys(self):
        a = self.user("a")
        self.loaded(a, platform="atcoder")
        self.loaded(a, platform="kilonova")
        g, asg = self.group([a])
        ac = self.item(asg, "atcoder", "contest", "abc100")
        self.db.add(models.ContestProblem(assignment_item_id=ac.id, platform_problem_id="abc100_a", index="A", name="A"))
        self.item(asg, "kilonova", "problem", "1234")
        self.db.commit()
        self.sub(a, "abc100_a", 1, platform="atcoder")
        self.sub(a, "1234", 1, platform="kilonova")
        self.sub(a, "abc100_b", 1, platform="atcoder")   # not in the assignment
        self.db.refresh(g)
        self.assertEqual(leaderboard.for_group(self.db, g)[0].solved, 2)

    def test_equal_counts_share_a_rank(self):
        us = [self.user(n) for n in ("c", "a", "b")]
        self.loaded(*us)
        for u, k in ((us[0], 1), (us[1], 2), (us[2], 2)):
            for i in range(k):
                self.sub(u, f"{u.id}/{i}", 1)
        rows = leaderboard.for_platform(self.db)
        self.assertEqual([(r.user.username, r.solved, r.rank) for r in rows], [("a", 2, 1), ("b", 2, 1), ("c", 1, 3)])

    def test_the_platform_board_skips_students_the_admin_and_people_with_no_new_solves(self):
        u = self.user("worker")
        idle, student, admin = self.user("idle"), self.user("pupil", user_type="student"), self.user("boss", user_type="admin")
        self.loaded(u, idle, student, admin)
        self.sub(u, "1/A", 1)
        self.sub(idle, "1/A", 50)
        self.sub(idle, "1/A", 1)
        self.sub(student, "1/A", 1)
        self.sub(admin, "1/A", 1)
        self.assertEqual(self.solved(leaderboard.for_platform(self.db)), {"worker": 1})

    def test_the_platform_board_is_capped(self):
        us = [self.user(f"u{i:02d}") for i in range(15)]
        self.loaded(*us)
        for u in us:
            self.sub(u, f"{u.id}/A", 1)
        self.assertEqual(len(leaderboard.for_platform(self.db)), leaderboard.GLOBAL_LIMIT)
