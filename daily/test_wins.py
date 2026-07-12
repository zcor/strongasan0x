"""Wins with north stars (goal-with-substeps): surface leaves only, complete a
goal when its last stone is done, the editor endpoints, and the week-strip star.
See daily/CLIMB_REFRAME_PLAN.md section 2b."""
import json

from django.test import TestCase, Client
from django.utils import timezone

from daily.auth import SESSION_DAILY_PARTICIPANT_ID
from daily.models import DailyParticipant, WinItem
from daily.services.wins import (
    add_win,
    complete_win,
    create_north_star,
    get_todays_win,
    north_star_done_dates,
    promote_to_habit,
)


class WinsServiceTests(TestCase):
    def setUp(self):
        self.p = DailyParticipant.objects.create(
            display_name="Tester", kind=DailyParticipant.KIND_EXTERNAL, beta=True
        )
        self.today = timezone.localdate()

    def test_north_star_surfaces_stones_not_the_goal(self):
        goal = create_north_star(
            self.p, "Get a new job", ["Update resume", "Apply to one job"]
        )
        self.assertIsNotNone(goal)
        self.assertTrue(goal.is_goal)
        self.assertEqual(goal.stones.count(), 2)

        # Surface-one returns a STONE, never the goal.
        surfaced = get_todays_win(self.p, self.today)
        self.assertFalse(surfaced.is_goal)
        self.assertEqual(surfaced.text, "Update resume")
        self.assertEqual(surfaced.parent_id, goal.id)

    def test_last_stone_completes_the_goal(self):
        goal = create_north_star(self.p, "Repaint bedroom", ["Pick color", "Do wall"])
        s1, s2 = list(goal.stones.order_by("order"))

        _, completed = complete_win(s1)
        self.assertIsNone(completed)  # one stone left → goal still open
        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_OPEN)

        _, completed = complete_win(s2)
        self.assertIsNotNone(completed)  # last stone → goal completes
        self.assertEqual(completed.id, goal.id)
        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_DONE)
        self.assertIsNotNone(goal.done_at)

    def test_standalone_win_unchanged(self):
        w = add_win(self.p, "Book the dentist")
        self.assertFalse(w.is_goal)
        self.assertIsNone(w.parent_id)
        surfaced = get_todays_win(self.p, self.today)
        self.assertEqual(surfaced.id, w.id)
        _, completed = complete_win(w)
        self.assertIsNone(completed)  # no parent → nothing to summit

    def test_star_date_tracks_goal_completion(self):
        goal = create_north_star(self.p, "Ship v1", ["Only step"])
        (stone,) = list(goal.stones.all())
        complete_win(stone)
        days = north_star_done_dates(self.p, self.today, self.today)
        self.assertIn(self.today, days)

    def test_empty_goal_rejected(self):
        self.assertIsNone(create_north_star(self.p, "", ["a"]))
        self.assertIsNone(create_north_star(self.p, "Goal", []))

    def test_promote_last_stone_completes_goal(self):
        # Graduating the last open stone must summit the goal exactly like
        # completing it would — otherwise the north star is stranded OPEN
        # with nothing left to surface.
        goal = create_north_star(self.p, "Get moving", ["Walk daily"])
        (stone,) = list(goal.stones.all())
        item, completed = promote_to_habit(stone, self.p)
        self.assertIsNotNone(item)
        self.assertEqual(completed.id, goal.id)
        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_DONE)
        stone.refresh_from_db()
        self.assertEqual(stone.status, WinItem.STATUS_GRADUATED)

    def test_promote_with_stones_left_keeps_goal_open(self):
        goal = create_north_star(self.p, "Get strong", ["Stretch", "Lift"])
        s1, _s2 = list(goal.stones.order_by("order"))
        item, completed = promote_to_habit(s1, self.p)
        self.assertIsNotNone(item)
        self.assertIsNone(completed)
        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_OPEN)

    def test_removing_last_stale_stone_completes_goal_with_progress(self):
        from daily.services.wins import remove_win

        goal = create_north_star(self.p, "Declutter", ["Closet", "Garage"])
        s1, s2 = list(goal.stones.order_by("order"))
        complete_win(s1)
        # The remaining stone turned out to be stale; deleting it is the last
        # open stone leaving the set — the goal must summit, not strand OPEN.
        completed = remove_win(s2)
        self.assertIsNotNone(completed)
        self.assertEqual(completed.id, goal.id)
        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_DONE)

    def test_removing_all_stones_of_untouched_goal_leaves_it_open(self):
        from daily.services.wins import remove_win

        goal = create_north_star(self.p, "Someday", ["Only step"])
        (stone,) = list(goal.stones.all())
        # Nothing was ever done toward this goal: deleting its only stone must
        # NOT fake an achievement; the user can add fresh stones later.
        self.assertIsNone(remove_win(stone))
        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_OPEN)


class WinsEndpointTests(TestCase):
    def setUp(self):
        self.p = DailyParticipant.objects.create(
            display_name="Tester", kind=DailyParticipant.KIND_EXTERNAL, beta=True
        )
        self.c = Client()
        s = self.c.session
        s[SESSION_DAILY_PARTICIPANT_ID] = self.p.id
        s.save()

    def _post(self, url, body):
        return self.c.post(url, data=json.dumps(body), content_type="application/json")

    def test_create_goal_add_stone_and_complete_via_endpoints(self):
        r = self._post("/daily/win/goal/add/", {"goal": "Get fit", "stones": ["Walk 20 min"]})
        self.assertEqual(r.status_code, 200)
        goal_id = r.json()["goal"]["id"]

        # Add a second stone via the editor endpoint.
        r2 = self._post("/daily/win/stone/add/", {"goal_id": goal_id, "text": "Meal prep"})
        self.assertEqual(r2.status_code, 200)
        stone2_id = r2.json()["stone"]["id"]

        # First stone is surfaced on the daily crown, with the goal beneath.
        surfaced_id = WinItem.objects.get(participant=self.p, text="Walk 20 min").id
        r3 = self._post("/daily/win/", {"id": surfaced_id, "action": "did_it"})
        self.assertEqual(r3.status_code, 200)
        self.assertNotIn("north_star_done", r3.json())  # one stone still open

        # Completing the last stone completes the goal → celebration signal.
        r4 = self._post("/daily/win/", {"id": stone2_id, "action": "did_it"})
        self.assertEqual(r4.json().get("north_star_done", {}).get("title"), "Get fit")

    def test_remove_goal_cascades_stones(self):
        r = self._post("/daily/win/goal/add/", {"goal": "Declutter", "stones": ["Closet", "Garage"]})
        goal_id = r.json()["goal"]["id"]
        self.assertEqual(WinItem.objects.filter(participant=self.p).count(), 3)
        r2 = self._post("/daily/win/remove/", {"id": goal_id})
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(WinItem.objects.filter(participant=self.p).count(), 0)

    def test_foreign_win_rejected(self):
        other = DailyParticipant.objects.create(
            display_name="Other", kind=DailyParticipant.KIND_EXTERNAL, beta=True
        )
        theirs = add_win(other, "Not yours")
        r = self._post("/daily/win/remove/", {"id": theirs.id})
        self.assertEqual(r.status_code, 404)
        self.assertTrue(WinItem.objects.filter(id=theirs.id).exists())

    def test_goal_cannot_be_acted_on_directly(self):
        # A north star only completes via its last stone; acting on the goal
        # id itself must 404, or the summit moment never fires.
        goal = create_north_star(self.p, "Big goal", ["Step 1"])
        r = self._post("/daily/win/", {"id": goal.id, "action": "did_it"})
        self.assertEqual(r.status_code, 404)
        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_OPEN)

    def test_promote_endpoint_signals_summit(self):
        goal = create_north_star(self.p, "Get fit", ["Walk 20 min"])
        stone = goal.stones.get()
        r = self._post("/daily/win/", {"id": stone.id, "action": "promote"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("item", body)
        self.assertEqual(body.get("north_star_done", {}).get("title"), "Get fit")

    def test_editor_page_renders(self):
        create_north_star(self.p, "Learn guitar", ["Buy picks"])
        r = self.c.get("/daily/wins/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Learn guitar")
        # No finished north star yet: no Achieved link, no achieved card.
        self.assertNotContains(r, "/daily/wins/achieved/")

    def test_achieved_page_shows_goal_with_steps(self):
        from daily.services.wins import promote_to_habit

        goal = create_north_star(self.p, "Run a 5k", ["Buy shoes", "Jog twice"])
        s1, s2 = list(goal.stones.order_by("order"))
        complete_win(s1)
        promote_to_habit(s2, self.p)  # graduating the last stone summits the goal

        # The editor now offers the quiet top-right link on Working toward.
        r = self.c.get("/daily/wins/")
        self.assertContains(r, "/daily/wins/achieved/")
        self.assertNotContains(r, "Run a 5k")  # finished goals left the editor

        r2 = self.c.get("/daily/wins/achieved/")
        self.assertEqual(r2.status_code, 200)
        self.assertContains(r2, "Run a 5k")
        self.assertContains(r2, "Buy shoes")
        self.assertContains(r2, "Jog twice")
        self.assertContains(r2, "now a habit")   # the graduated stone's tag
        self.assertContains(r2, "/daily/wins/")  # back link to the editor

    def test_achieved_page_is_beta_gated(self):
        self.p.beta = False
        self.p.save(update_fields=["beta"])
        r = self.c.get("/daily/wins/achieved/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], "/daily/checkin/")

    def test_beta_checkin_renders_stone_with_goal(self):
        # The daily beta screen must render the surfaced stone with its north
        # star as "part of: ..." and the quiet "your list" door — exercises the
        # week-strip star loop and crown template edits.
        self.p.onboarded_at = timezone.now()  # skip onboarding → real check-in
        self.p.save(update_fields=["onboarded_at"])
        create_north_star(self.p, "Run a 5k", ["Lace up and jog once"])
        r = self.c.get("/daily/checkin/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Lace up and jog once")  # surfaced stone title
        self.assertContains(r, "Run a 5k")              # part of: goal
        self.assertContains(r, "/daily/wins/")          # the door
