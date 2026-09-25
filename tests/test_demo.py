"""The offline demo (scripts/demo.py) seeds a sensible, deterministic world."""
from contextlib import ExitStack

from app import models
from scripts import demo
from tests.support import DbTestCase


class DemoSeed(DbTestCase):
    async def test_seed_builds_the_showcase(self):
        world = demo.World(now=1_790_000_000)
        with ExitStack() as stack:
            demo.install_fakes(world, stack)
            await demo.seed(self.db)

        items = self.db.query(models.AssignmentItem).all()
        self.assertEqual(len(items), 13)
        self.assertEqual({i.sync_status for i in items}, {"done"})  # nothing errors or stays pending

        group = self.db.query(models.Group).one()
        self.assertEqual((group.owner.username, group.max_members), ("ioana", 10))
        gym = next(i for i in items if i.external_id == "105427")
        by_user = {r.user.username: r for r in self.db.query(models.Result).filter_by(assignment_item_id=gym.id)}
        for name in demo.GYM_TEAM_MEMBERS:  # the team's virtual counts for every member
            self.assertEqual((by_user[name].problems_solved_count, by_user[name].problems_total_count), (len(demo.GYM_RESULT), 11))
        self.assertFalse(by_user["radu"].participated)

        rounds = [i for i in items if i.external_id in demo.CF_CONTESTS]
        self.assertTrue(all(len(r.contest_problems) >= 6 for r in rounds))
        self.assertTrue(self.db.query(models.Result).filter(models.Result.rating_change.isnot(None)).count() > 0)
        kinds = {p.solve_type for p in self.db.query(models.ProblemResult)}
        self.assertTrue({"live", "virtual", "upsolving"} <= kinds)
        self.assertGreater(self.db.query(models.Submission).count(), 500)
        self.assertEqual(self.db.query(models.Hint).count(), 2)

    def test_the_world_is_deterministic(self):
        a, b = demo.World(now=1_790_000_000), demo.World(now=1_790_000_000)
        self.assertEqual(a.cf, b.cf)
        self.assertEqual(a.ac, b.ac)
        self.assertEqual(a.kn, b.kn)
