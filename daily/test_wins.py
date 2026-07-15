"""Wins with explicit daily selection and user-completed North Stars."""
import json

from django.db import connection
from django.test import TestCase, Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from daily.auth import SESSION_DAILY_PARTICIPANT_ID
from daily.models import DailyParticipant, WinItem
from daily.services.wins import (
    add_win,
    archive_goal,
    complete_goal,
    complete_win,
    create_north_star,
    get_todays_win,
    list_backlog,
    promote_to_habit,
    restore_goal,
    select_todays_win,
    todays_win_done_dates,
    uncomplete_win,
)


class WinsServiceTests(TestCase):
    def setUp(self):
        self.p = DailyParticipant.objects.create(
            display_name="Tester", kind=DailyParticipant.KIND_EXTERNAL, beta=True
        )
        self.today = timezone.localdate()

    def test_north_star_waits_for_explicit_stone_selection(self):
        goal = create_north_star(
            self.p, "Get a new job", ["Update resume", "Apply to one job"]
        )
        self.assertIsNotNone(goal)
        self.assertTrue(goal.is_goal)
        self.assertEqual(goal.stones.count(), 2)

        # Nothing appears at random. The selected leaf, never the goal, appears.
        self.assertIsNone(get_todays_win(self.p, self.today))
        stone = goal.stones.order_by("order").first()
        select_todays_win(self.p, stone, self.today)
        surfaced = get_todays_win(self.p, self.today)
        self.assertFalse(surfaced.is_goal)
        self.assertEqual(surfaced.text, "Update resume")
        self.assertEqual(surfaced.parent_id, goal.id)

    def test_last_stone_enables_explicit_goal_completion(self):
        goal = create_north_star(self.p, "Repaint bedroom", ["Pick color", "Do wall"])
        s1, s2 = list(goal.stones.order_by("order"))

        _, completed = complete_win(s1)
        self.assertIsNone(completed)  # one stone left → goal still open
        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_OPEN)

        _, completed = complete_win(s2)
        self.assertIsNone(completed)
        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_OPEN)

        completed = complete_goal(goal)
        self.assertEqual(completed.id, goal.id)
        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_DONE)

    def test_standalone_win_unchanged(self):
        w = add_win(self.p, "Book the dentist")
        self.assertFalse(w.is_goal)
        self.assertIsNone(w.parent_id)
        self.assertIsNone(get_todays_win(self.p, self.today))
        select_todays_win(self.p, w, self.today)
        surfaced = get_todays_win(self.p, self.today)
        self.assertEqual(surfaced.id, w.id)
        _, completed = complete_win(w, featured_on=self.today)
        self.assertIsNone(completed)  # no parent → nothing to summit

    def test_selecting_standalone_does_not_join_nullable_parent_while_locking(self):
        standalone = add_win(self.p, "Call parents")

        with CaptureQueriesContext(connection) as queries:
            selected = select_todays_win(self.p, standalone, self.today)

        self.assertEqual(selected.id, standalone.id)
        locking_select = next(
            query["sql"] for query in queries.captured_queries
            if 'FROM "daily_winitem"' in query["sql"] and "SELECT" in query["sql"]
        )
        self.assertNotIn('JOIN "daily_winitem"', locking_select)

    def test_star_date_tracks_only_a_completed_selected_win(self):
        goal = create_north_star(self.p, "Ship v1", ["Only step"])
        (stone,) = list(goal.stones.all())
        complete_win(stone)
        self.assertNotIn(
            self.today, todays_win_done_dates(self.p, self.today, self.today)
        )

        standalone = add_win(self.p, "Send the invoice")
        select_todays_win(self.p, standalone, self.today)
        complete_win(standalone, featured_on=self.today)
        days = todays_win_done_dates(self.p, self.today, self.today)
        self.assertIn(self.today, days)

    def test_empty_goal_rejected(self):
        self.assertIsNone(create_north_star(self.p, "", ["a"]))
        self.assertIsNone(create_north_star(self.p, "Goal", []))

    def test_promote_last_stone_waits_for_explicit_goal_completion(self):
        goal = create_north_star(self.p, "Get moving", ["Walk daily"])
        (stone,) = list(goal.stones.all())
        item, completed = promote_to_habit(stone, self.p)
        self.assertIsNotNone(item)
        self.assertIsNone(completed)
        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_OPEN)
        stone.refresh_from_db()
        self.assertEqual(stone.status, WinItem.STATUS_GRADUATED)
        self.assertEqual(complete_goal(goal).id, goal.id)

    def test_promote_with_stones_left_keeps_goal_open(self):
        goal = create_north_star(self.p, "Get strong", ["Stretch", "Lift"])
        s1, _s2 = list(goal.stones.order_by("order"))
        item, completed = promote_to_habit(s1, self.p)
        self.assertIsNotNone(item)
        self.assertIsNone(completed)
        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_OPEN)

    def test_removing_last_stale_stone_still_requires_complete(self):
        from daily.services.wins import remove_win

        goal = create_north_star(self.p, "Declutter", ["Closet", "Garage"])
        s1, s2 = list(goal.stones.order_by("order"))
        complete_win(s1)
        completed = remove_win(s2)
        self.assertIsNone(completed)
        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_OPEN)
        self.assertEqual(complete_goal(goal).id, goal.id)

    def test_removing_all_stones_of_untouched_goal_leaves_it_open(self):
        from daily.services.wins import remove_win

        goal = create_north_star(self.p, "Someday", ["Only step"])
        (stone,) = list(goal.stones.all())
        # Nothing was ever done toward this goal: deleting its only stone must
        # NOT fake an achievement; the user can add fresh stones later.
        self.assertIsNone(remove_win(stone))
        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_OPEN)

    def test_checked_steps_remain_in_open_north_star_backlog(self):
        goal = create_north_star(self.p, "Learn Spanish", ["Book tutor", "Practice"])
        first = goal.stones.order_by("order").first()
        complete_win(first)

        backlog = list_backlog(self.p)
        rendered_goal = backlog["goals"][0]
        self.assertEqual(len(rendered_goal["stones"]), 2)
        self.assertEqual(rendered_goal["stones"][0].status, WinItem.STATUS_DONE)
        self.assertEqual(rendered_goal["open_count"], 1)
        self.assertFalse(rendered_goal["can_complete"])

    def test_checked_step_can_be_reopened(self):
        goal = create_north_star(self.p, "Prepare portfolio", ["Choose samples"])
        stone = goal.stones.get()
        select_todays_win(self.p, stone, self.today)
        complete_win(stone, featured_on=self.today)
        self.assertIn(
            self.today, todays_win_done_dates(self.p, self.today, self.today)
        )

        reopened = uncomplete_win(stone, self.today)
        self.assertEqual(reopened.status, WinItem.STATUS_OPEN)
        self.assertEqual(reopened.surfaced_on, self.today)
        self.assertIsNone(reopened.done_at)
        self.assertNotIn(
            self.today, todays_win_done_dates(self.p, self.today, self.today)
        )

    def test_archived_goal_is_restorable_with_steps_intact(self):
        goal = create_north_star(self.p, "Plan a trip", ["Pick dates", "Book room"])
        first = goal.stones.order_by("order").first()
        complete_win(first)

        archived = archive_goal(goal)
        self.assertEqual(archived.status, WinItem.STATUS_ARCHIVED)
        self.assertEqual(archived.stones.count(), 2)

        restored = restore_goal(archived)
        self.assertEqual(restored.status, WinItem.STATUS_OPEN)
        self.assertEqual(
            list(restored.stones.order_by("order").values_list("status", flat=True)),
            [WinItem.STATUS_DONE, WinItem.STATUS_OPEN],
        )


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
        self.assertIsNone(r.json()["next"])
        goal_id = r.json()["goal"]["id"]

        # Add a second stone via the editor endpoint.
        r2 = self._post("/daily/win/stone/add/", {"goal_id": goal_id, "text": "Meal prep"})
        self.assertEqual(r2.status_code, 200)
        self.assertIsNone(r2.json()["next"])
        stone2_id = r2.json()["stone"]["id"]

        # The user explicitly chooses the first step for today.
        surfaced_id = WinItem.objects.get(participant=self.p, text="Walk 20 min").id
        selected = self._post("/daily/win/select/", {"id": surfaced_id})
        self.assertEqual(selected.json()["next"]["title"], "Walk 20 min")
        r3 = self._post("/daily/win/", {"id": surfaced_id, "action": "did_it"})
        self.assertEqual(r3.status_code, 200)
        self.assertTrue(r3.json()["featured_done"])

        # Checking the last step enables, but does not auto-complete, the group.
        r4 = self._post("/daily/win/", {"id": stone2_id, "action": "check_off"})
        self.assertNotIn("north_star_done", r4.json())
        goal = WinItem.objects.get(id=goal_id)
        self.assertEqual(goal.status, WinItem.STATUS_OPEN)
        r5 = self._post("/daily/win/goal/complete/", {"id": goal_id})
        self.assertEqual(r5.json()["north_star_done"]["title"], "Get fit")

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
        # A North Star completes only through its explicit group endpoint.
        goal = create_north_star(self.p, "Big goal", ["Step 1"])
        r = self._post("/daily/win/", {"id": goal.id, "action": "did_it"})
        self.assertEqual(r.status_code, 404)
        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_OPEN)

    def test_goal_complete_is_disabled_server_side_until_all_steps_are_done(self):
        goal = create_north_star(self.p, "Big goal", ["Step 1", "Step 2"])
        r = self._post("/daily/win/goal/complete/", {"id": goal.id})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "steps_remaining")

    def test_unselected_win_cannot_be_completed_as_todays_win(self):
        win = add_win(self.p, "File paperwork")
        r = self._post("/daily/win/", {"id": win.id, "action": "did_it"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "not_selected")

    def test_checked_north_star_step_can_be_unchecked(self):
        goal = create_north_star(self.p, "Prepare portfolio", ["Choose samples"])
        stone = goal.stones.get()
        selected = self._post("/daily/win/select/", {"id": stone.id})
        self.assertEqual(selected.status_code, 200)
        checked = self._post("/daily/win/", {"id": stone.id, "action": "did_it"})
        self.assertTrue(checked.json()["today_has_completed_win"])

        editor = self.c.get("/daily/wins/")
        self.assertContains(editor, f'data-uncheck-step="{stone.id}"')
        self.assertContains(editor, "0 left")

        unchecked = self._post("/daily/win/", {"id": stone.id, "action": "uncheck"})
        self.assertEqual(unchecked.status_code, 200)
        body = unchecked.json()
        self.assertEqual(body["reopened"]["status"], WinItem.STATUS_OPEN)
        self.assertTrue(body["reopened"]["selected_today"])
        self.assertFalse(body["today_has_completed_win"])
        self.assertEqual(body["next"]["id"], stone.id)

        blocked = self._post("/daily/win/goal/complete/", {"id": goal.id})
        self.assertEqual(blocked.status_code, 409)

    def test_promote_endpoint_leaves_north_star_ready_to_complete(self):
        goal = create_north_star(self.p, "Get fit", ["Walk 20 min"])
        stone = goal.stones.get()
        r = self._post("/daily/win/", {"id": stone.id, "action": "promote"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("item", body)
        self.assertNotIn("north_star_done", body)
        self.assertEqual(goal.stones.get().status, WinItem.STATUS_GRADUATED)
        self.assertEqual(
            self._post("/daily/win/goal/complete/", {"id": goal.id}).status_code,
            200,
        )

    def test_editor_page_renders(self):
        create_north_star(self.p, "Learn guitar", ["Buy picks"])
        r = self.c.get("/daily/wins/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Learn guitar")
        self.assertContains(r, "Pick today")
        self.assertContains(r, "data-complete-goal")
        self.assertContains(r, "Add a step")
        self.assertContains(r, "data-archive-goal")
        self.assertContains(r, "/daily/wins/archived/")
        self.assertContains(r, 'class="weback" href="/daily/checkin/"')
        # No finished north star yet: no Achieved link, no achieved card.
        self.assertNotContains(r, "/daily/wins/achieved/")

        fragment = self.c.get("/daily/wins/?fragment=1")
        self.assertTemplateUsed(fragment, "daily/_wins_editor.html")
        self.assertContains(fragment, "Learn guitar")
        self.assertContains(fragment, "data-wins-close")
        self.assertNotContains(fragment, "<!doctype html>", html=False)

    def test_one_off_actions_render_in_divided_footer_with_clear_labels(self):
        add_win(self.p, "Call parents regarding flight info")

        response = self.c.get("/daily/wins/")

        self.assertContains(response, 'class="single-main"')
        self.assertContains(response, 'class="single-actions"')
        self.assertContains(response, ">Pick today</button>")
        self.assertContains(response, ">Delete</button>")
        self.assertContains(response, ">Move to habit</button>")
        self.assertNotContains(response, "→ habit")

    def test_three_north_stars_render_collapsed(self):
        create_north_star(self.p, "Goal one", ["Step one"])
        create_north_star(self.p, "Goal two", ["Step two"])
        create_north_star(self.p, "Goal three", ["Step three"])
        r = self.c.get("/daily/wins/")
        self.assertContains(r, 'class="wins-editor many-goals"')
        self.assertContains(r, 'class="goal collapsible collapsed"', count=3)
        self.assertContains(r, 'data-toggle-goal aria-expanded="false"', count=3)

    def test_archive_endpoint_moves_goal_without_deleting_it_and_restore_returns_it(self):
        goal = create_north_star(self.p, "Plan a trip", ["Pick dates", "Book room"])
        stone_ids = list(goal.stones.values_list("id", flat=True))

        archived = self._post("/daily/win/goal/archive/", {"id": goal.id})
        self.assertEqual(archived.status_code, 200)
        self.assertEqual(archived.json()["archived"]["title"], "Plan a trip")
        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_ARCHIVED)
        self.assertEqual(list(goal.stones.values_list("id", flat=True)), stone_ids)

        page = self.c.get("/daily/wins/archived/")
        self.assertContains(page, "Plan a trip")
        self.assertContains(page, "Pick dates")
        self.assertContains(page, "Restore")

        fragment = self.c.get("/daily/wins/archived/?fragment=1")
        self.assertEqual(fragment.status_code, 200)
        self.assertTemplateUsed(fragment, "daily/_wins_archived_panel.html")
        self.assertContains(fragment, "data-wins-archived-content")
        self.assertContains(fragment, "data-archived-back")
        self.assertNotContains(fragment, "<!doctype html>", html=False)

        restored = self.c.post("/daily/win/goal/restore/", {"id": goal.id})
        self.assertEqual(restored.status_code, 302)
        self.assertEqual(restored.headers["Location"], "/daily/wins/archived/")
        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_OPEN)

        self._post("/daily/win/goal/archive/", {"id": goal.id})
        restored_json = self._post("/daily/win/goal/restore/", {"id": goal.id})
        self.assertEqual(restored_json.status_code, 200)
        self.assertEqual(restored_json.json()["goal"]["text"], "Plan a trip")
        self.assertEqual(len(restored_json.json()["goal"]["stones"]), 2)

    def test_add_win_returns_created_row_and_today_state(self):
        r = self._post("/daily/win/add/", {"text": "Book the dentist"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["added"]["text"], "Book the dentist")
        self.assertIsNone(body["next"])
        selected = self._post("/daily/win/select/", {"id": body["added"]["id"]})
        self.assertEqual(selected.json()["next"]["id"], body["added"]["id"])

    def test_completed_todays_win_remains_visible_with_success_copy(self):
        self.p.onboarded_at = timezone.now()
        self.p.save(update_fields=["onboarded_at"])
        win = add_win(self.p, "Send thank-you note")
        selected = self._post("/daily/win/select/", {"id": win.id})
        self.assertEqual(selected.status_code, 200)
        completed = self._post("/daily/win/", {"id": win.id, "action": "did_it"})
        self.assertTrue(completed.json()["featured_done"])

        r = self.c.get("/daily/checkin/")
        self.assertContains(r, "Send thank-you note")
        self.assertContains(r, "Great job!")
        self.assertContains(r, "aria-label=\"completed today's win\"")
        self.assertContains(r, 'class="win-success" id="win-success"')

    def test_achieved_page_shows_goal_with_steps(self):
        from daily.services.wins import promote_to_habit

        goal = create_north_star(self.p, "Run a 5k", ["Buy shoes", "Jog twice"])
        s1, s2 = list(goal.stones.order_by("order"))
        complete_win(s1)
        promote_to_habit(s2, self.p)
        complete_goal(goal)

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
        stone = self.p.wins.get(text="Lace up and jog once")
        select_todays_win(self.p, stone, timezone.localdate())
        r = self.c.get("/daily/checkin/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Lace up and jog once")  # surfaced stone title
        self.assertContains(r, "Run a 5k")              # part of: goal
        self.assertContains(r, 'id="wins-dialog-open"')
        self.assertContains(r, 'id="wins-dialog"')
        self.assertContains(r, 'id="wins-dialog-archived"')
        self.assertContains(r, "data-wins-loading")
        self.assertContains(r, "data-wins-host")
        self.assertContains(r, "data-wins-archived-loading")
        self.assertContains(r, 'data-wins-loading-close')
        self.assertNotContains(r, "{# The full wins editor")
        self.assertContains(r, "/static/daily/images/jamie-avatar.jpg")
        self.assertContains(r, "Add to Home Screen")
        self.assertContains(r, "Turn on notifications")
        self.assertNotContains(r, "notification-nudge-bell")
