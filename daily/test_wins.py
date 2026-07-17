"""Wins with automatic/explicit daily selection and completed North Stars."""
import json
from datetime import timedelta

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
    get_dashboard_wins,
    get_todays_win,
    list_backlog,
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

        # The low-level getter does not mutate. The dashboard owns automatic
        # selection; an explicit selection still chooses any requested leaf.
        self.assertIsNone(get_todays_win(self.p, self.today))
        stone = goal.stones.order_by("order").first()
        select_todays_win(self.p, stone, self.today)
        surfaced = get_todays_win(self.p, self.today)
        self.assertFalse(surfaced.is_goal)
        self.assertEqual(surfaced.text, "Update resume")
        self.assertEqual(surfaced.parent_id, goal.id)

    def test_dashboard_fills_empty_card_from_first_open_win(self):
        first = add_win(self.p, "Book the dentist")
        add_win(self.p, "Call the plumber")

        state = get_dashboard_wins(
            self.p, self.today - timedelta(days=6), self.today,
        )

        self.assertEqual(state["selected"].id, first.id)
        self.assertEqual(get_todays_win(self.p, self.today).id, first.id)

    def test_past_day_dashboard_does_not_auto_select(self):
        add_win(self.p, "Book the dentist")

        state = get_dashboard_wins(
            self.p,
            self.today - timedelta(days=7),
            self.today - timedelta(days=1),
            auto_select=False,
        )

        self.assertIsNone(state["selected"])
        self.assertIsNone(get_todays_win(self.p, self.today))

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

    def test_uncheck_defers_to_a_newer_selection(self):
        """Reopening a completed win must not steal today's selection back
        from a win the user explicitly picked afterwards."""
        goal = create_north_star(self.p, "Two steps", ["A", "B"])
        a, b = list(goal.stones.order_by("order"))
        select_todays_win(self.p, a, self.today)
        complete_win(a, featured_on=self.today)
        select_todays_win(self.p, b, self.today)

        reopened = uncomplete_win(a, self.today)
        self.assertIsNone(reopened.surfaced_on)
        self.assertEqual(get_todays_win(self.p, self.today).id, b.id)

    def test_add_stone_cap_counts_only_open_steps(self):
        """Finished steps are history; they never block adding the next one."""
        from unittest.mock import patch
        from daily.services.wins import add_stone

        goal = create_north_star(self.p, "Long haul", ["one", "two"])
        s1, _s2 = list(goal.stones.order_by("order"))
        complete_win(s1)
        with patch("daily.services.wins.MAX_STONES_PER_GOAL", 2):
            self.assertIsNotNone(add_stone(self.p, goal, "three"))  # 1 open < 2
            self.assertIsNone(add_stone(self.p, goal, "four"))      # 2 open = cap

    def test_empty_goal_rejected(self):
        self.assertIsNone(create_north_star(self.p, "", ["a"]))
        self.assertIsNone(create_north_star(self.p, "Goal", []))

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
        self.assertEqual(r.json()["next"]["title"], "Walk 20 min")
        goal_id = r.json()["goal"]["id"]

        # Add a second stone via the editor endpoint.
        r2 = self._post("/daily/win/stone/add/", {"goal_id": goal_id, "text": "Meal prep"})
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["next"]["title"], "Walk 20 min")
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
        self.assertTrue(r4.json()["completed_today"]["goal_can_complete"])
        self.assertEqual(r4.json()["completed_today"]["goal_id"], goal_id)
        goal = WinItem.objects.get(id=goal_id)
        self.assertEqual(goal.status, WinItem.STATUS_OPEN)
        r5 = self._post("/daily/win/goal/complete/", {"id": goal_id})
        self.assertEqual(r5.json()["north_star_done"]["title"], "Get fit")

    def test_active_north_star_can_be_renamed(self):
        goal = create_north_star(self.p, "Find a job", ["Update resume"])

        renamed = self._post(
            "/daily/win/goal/edit/",
            {"id": goal.id, "text": "Find a design job"},
        )

        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["goal"]["text"], "Find a design job")
        goal.refresh_from_db()
        self.assertEqual(goal.text, "Find a design job")

        empty = self._post("/daily/win/goal/edit/", {"id": goal.id, "text": "  "})
        self.assertEqual(empty.status_code, 400)
        goal.refresh_from_db()
        self.assertEqual(goal.text, "Find a design job")

    def test_open_stone_can_be_renamed(self):
        goal = create_north_star(self.p, "Find a job", ["Update resume"])
        stone = goal.stones.get()

        renamed = self._post(
            "/daily/win/stone/edit/", {"id": stone.id, "text": "Update the resume"}
        )

        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["stone"]["text"], "Update the resume")
        stone.refresh_from_db()
        self.assertEqual(stone.text, "Update the resume")

        empty = self._post("/daily/win/stone/edit/", {"id": stone.id, "text": "  "})
        self.assertEqual(empty.status_code, 400)
        stone.refresh_from_db()
        self.assertEqual(stone.text, "Update the resume")

    def test_done_or_foreign_stone_cannot_be_renamed(self):
        goal = create_north_star(self.p, "Find a job", ["Update resume"])
        stone = goal.stones.get()
        stone.status = WinItem.STATUS_DONE
        stone.save(update_fields=["status"])
        done = self._post("/daily/win/stone/edit/", {"id": stone.id, "text": "Changed"})
        self.assertEqual(done.status_code, 404)

        other = DailyParticipant.objects.create(
            display_name="Other", kind=DailyParticipant.KIND_EXTERNAL, beta=True
        )
        their_goal = create_north_star(other, "Private goal", ["Private step"])
        their_stone = their_goal.stones.get()
        foreign = self._post(
            "/daily/win/stone/edit/", {"id": their_stone.id, "text": "Changed"}
        )
        self.assertEqual(foreign.status_code, 404)
        their_stone.refresh_from_db()
        self.assertEqual(their_stone.text, "Private step")

    def test_cannot_rename_another_participants_north_star(self):
        other = DailyParticipant.objects.create(
            display_name="Other", kind=DailyParticipant.KIND_EXTERNAL, beta=True
        )
        goal = create_north_star(other, "Private goal", ["Private step"])

        response = self._post(
            "/daily/win/goal/edit/", {"id": goal.id, "text": "Changed"}
        )

        self.assertEqual(response.status_code, 404)
        goal.refresh_from_db()
        self.assertEqual(goal.text, "Private goal")

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
        self.assertContains(editor, 'class="stone done"')
        self.assertContains(editor, f'data-uncheck-step="{stone.id}"')
        self.assertContains(editor, ">Uncheck</button>")
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

    def test_completed_todays_one_off_win_can_be_unchecked(self):
        win = add_win(self.p, "Call the bank")
        selected = self._post("/daily/win/select/", {"id": win.id})
        self.assertEqual(selected.status_code, 200)
        checked = self._post("/daily/win/", {"id": win.id, "action": "did_it"})
        self.assertTrue(checked.json()["today_has_completed_win"])

        unchecked = self._post("/daily/win/", {"id": win.id, "action": "uncheck"})
        self.assertEqual(unchecked.status_code, 200)
        body = unchecked.json()
        self.assertEqual(body["reopened"]["status"], WinItem.STATUS_OPEN)
        self.assertTrue(body["reopened"]["selected_today"])
        self.assertFalse(body["today_has_completed_win"])
        self.assertEqual(body["next"]["id"], win.id)

    def test_candidates_lists_open_steps_then_singles_for_the_picker(self):
        self.p.onboarded_at = timezone.now()
        self.p.save(update_fields=["onboarded_at"])
        goal = create_north_star(self.p, "Launch shop", ["Register domain", "Build page"])
        first, second = list(goal.stones.order_by("order", "created_at"))
        single = add_win(self.p, "Call the dentist")
        archived = create_north_star(self.p, "Old goal", ["Old step"])
        archive_goal(archived)
        complete_win(second)

        r = self.c.get("/daily/win/candidates/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        # Open North Star steps first (with their goal), then one-off wins.
        # Done steps and steps of archived goals never appear.
        self.assertEqual(
            [(c["text"], c["goal"]) for c in body["candidates"]],
            [("Register domain", "Launch shop"), ("Call the dentist", "")],
        )

        page = self.c.get("/daily/checkin/")
        self.assertContains(page, 'id="win-picker-dialog"')
        self.assertContains(page, "/daily/win/candidates/")

    def test_promote_action_is_gone(self):
        win = add_win(self.p, "Stretch for five minutes")
        r = self._post("/daily/win/", {"id": win.id, "action": "promote"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "bad_action")
        win.refresh_from_db()
        self.assertEqual(win.status, WinItem.STATUS_OPEN)

    def test_editor_page_renders(self):
        create_north_star(self.p, "Learn guitar", ["Buy picks"])
        r = self.c.get("/daily/wins/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Learn guitar")
        # Picking today's win lives in the Today card's picker, not the editor.
        self.assertNotContains(r, "Pick today")
        self.assertNotContains(r, "data-complete-goal")
        self.assertContains(r, "data-goal-title-input")
        self.assertContains(r, "data-save-goal")
        self.assertContains(r, "/daily/win/goal/edit/")
        self.assertContains(r, "Add a step")
        self.assertContains(r, "data-archive-goal")
        self.assertContains(r, "aria-label=\"Delete Learn guitar\"")
        self.assertContains(r, ">Delete</button>")
        self.assertContains(r, "/daily/wins/archived/")
        self.assertContains(r, 'class="weback" href="/daily/checkin/"')
        self.assertNotContains(r, "/daily/wins/achieved/")

        fragment = self.c.get("/daily/wins/?fragment=1")
        self.assertTemplateUsed(fragment, "daily/_wins_editor.html")
        self.assertContains(fragment, "Learn guitar")
        self.assertContains(fragment, "data-wins-close")
        self.assertNotContains(fragment, "<!doctype html>", html=False)

    def test_one_off_rows_render_as_expandable_rows_without_promote(self):
        add_win(self.p, "Call parents regarding flight info")

        response = self.c.get("/daily/wins/")

        # One-off wins share the list and the row anatomy with north stars:
        # chevron toggle, rename input, and Delete | Add a step | Save.
        self.assertContains(response, 'class="elist-item single collapsible collapsed"')
        self.assertContains(response, 'data-toggle-goal aria-label="Toggle Call parents regarding flight info"')
        self.assertContains(response, 'aria-label="Delete Call parents regarding flight info"')
        self.assertContains(response, 'data-add-stone')
        self.assertContains(response, 'data-save-goal')
        # "Move to habit" is gone, along with the row select circles.
        self.assertNotContains(response, "Move to habit")
        self.assertNotContains(response, "data-select-win")
        self.assertNotContains(response, 'id="promote-selected-btn"')
        self.assertNotContains(response, "Pick today")

    def test_one_off_win_can_be_renamed_through_the_goal_edit_endpoint(self):
        win = add_win(self.p, "Book the dentist")
        r = self._post("/daily/win/goal/edit/", {"id": win.id, "text": "Book the good dentist"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["goal"]["text"], "Book the good dentist")
        win.refresh_from_db()
        self.assertEqual(win.text, "Book the good dentist")
        self.assertFalse(win.is_goal)

    def test_adding_a_step_grows_a_one_off_win_into_a_north_star(self):
        win = add_win(self.p, "Get a new job")
        self._post("/daily/win/select/", {"id": win.id})

        r = self._post("/daily/win/stone/add/", {"goal_id": win.id, "text": "Update the resume"})

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["converted"])
        self.assertEqual(body["goal"], {"id": win.id, "text": "Get a new job"})
        self.assertEqual(body["stone"]["text"], "Update the resume")
        win.refresh_from_db()
        self.assertTrue(win.is_goal)
        # A goal is never itself the daily leaf; the selection cleared.
        self.assertIsNone(win.surfaced_on)
        stone = win.stones.get()
        self.assertEqual(stone.text, "Update the resume")
        self.assertEqual(stone.status, WinItem.STATUS_OPEN)

        # An empty first step must not convert anything.
        other = add_win(self.p, "Call the plumber")
        bad = self._post("/daily/win/stone/add/", {"goal_id": other.id, "text": "   "})
        self.assertEqual(bad.status_code, 400)
        other.refresh_from_db()
        self.assertFalse(other.is_goal)

    def test_editor_renders_one_combined_wins_section(self):
        create_north_star(self.p, "Learn guitar", ["Buy picks"])
        add_win(self.p, "Book the dentist")

        r = self.c.get("/daily/wins/")

        self.assertContains(r, 'id="wins-list"', count=1)
        self.assertContains(r, 'id="wins-card"')
        self.assertNotContains(r, 'id="goals-card"')
        self.assertNotContains(r, 'id="singles-card"')
        self.assertNotContains(r, ">One-off wins</div>")
        # One footer serves both shapes: the composer's optional steps decide
        # whether "Add a win" creates a one-off or a north star.
        self.assertContains(r, ">Add a win</button>")
        self.assertNotContains(r, ">Add a north star</button>")
        self.assertContains(r, 'id="ncomp-steps-section"')

    def test_north_stars_render_initially_collapsed(self):
        create_north_star(self.p, "Only goal", ["Only step"])
        one = self.c.get("/daily/wins/")
        self.assertContains(one, 'class="elist-item goal collapsible collapsed"', count=1)
        self.assertContains(one, 'data-toggle-goal aria-label="Toggle Only goal" aria-expanded="false"')

        create_north_star(self.p, "Goal one", ["Step one"])
        create_north_star(self.p, "Goal two", ["Step two"])
        r = self.c.get("/daily/wins/")
        self.assertContains(r, 'class="elist-item goal collapsible collapsed"', count=3)
        self.assertContains(r, 'class="goal-toggle elist-toggle" type="button" data-toggle-goal')

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
        # Achieved and archived wins share one history section.
        self.assertContains(page, '<div class="eyebrow">Wins</div>', html=True, count=1)
        self.assertNotContains(page, '<div class="eyebrow">Achieved</div>', html=True)
        self.assertNotContains(page, '<div class="eyebrow">Archived</div>', html=True)
        self.assertContains(page, "Plan a trip")
        self.assertContains(page, "Pick dates")
        self.assertContains(page, "Move to Working Toward")

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

    def test_restore_form_moves_every_checked_goal(self):
        first = create_north_star(self.p, "Goal A", ["a"])
        second = create_north_star(self.p, "Goal B", ["b"])
        archive_goal(first)
        archive_goal(second)

        page = self.c.get("/daily/wins/archived/")
        self.assertContains(page, 'class="goal-select"', count=2)
        self.assertContains(page, "Move to Working Toward", count=1)

        r = self.c.post("/daily/win/goal/restore/", {"id": [first.id, second.id]})
        self.assertEqual(r.status_code, 302)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, WinItem.STATUS_OPEN)
        self.assertEqual(second.status, WinItem.STATUS_OPEN)

        empty_submit = self.c.post("/daily/win/goal/restore/", {})
        self.assertEqual(empty_submit.status_code, 302)

    def test_add_win_returns_created_row_and_today_state(self):
        r = self._post("/daily/win/add/", {"text": "Book the dentist"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["added"]["text"], "Book the dentist")
        self.assertEqual(body["next"]["id"], body["added"]["id"])
        selected = self._post("/daily/win/select/", {"id": body["added"]["id"]})
        self.assertEqual(selected.json()["next"]["id"], body["added"]["id"])

    def test_not_today_advances_to_another_open_win(self):
        first = self._post("/daily/win/add/", {"text": "Book the dentist"}).json()["added"]
        second = self._post("/daily/win/add/", {"text": "Call the plumber"}).json()["added"]

        response = self._post(
            "/daily/win/", {"id": first["id"], "action": "not_today"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["next"]["id"], second["id"])

    def test_not_today_keeps_card_filled_when_only_one_win_exists(self):
        only = self._post("/daily/win/add/", {"text": "Book the dentist"}).json()["added"]

        response = self._post(
            "/daily/win/", {"id": only["id"], "action": "not_today"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["next"]["id"], only["id"])

    def test_dashboard_renders_an_open_win_without_manual_pick(self):
        self.p.onboarded_at = timezone.now()
        self.p.save(update_fields=["onboarded_at"])
        win = add_win(self.p, "Send the application")

        response = self.c.get("/daily/checkin/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Send the application")
        win.refresh_from_db()
        self.assertEqual(win.surfaced_on, timezone.localdate())

    def test_completed_todays_win_remains_visible_with_another_prompt(self):
        self.p.onboarded_at = timezone.now()
        self.p.save(update_fields=["onboarded_at"])
        win = add_win(self.p, "Send thank-you note")
        selected = self._post("/daily/win/select/", {"id": win.id})
        self.assertEqual(selected.status_code, 200)
        completed = self._post("/daily/win/", {"id": win.id, "action": "did_it"})
        self.assertTrue(completed.json()["featured_done"])

        r = self.c.get("/daily/checkin/")
        self.assertContains(r, "Send thank-you note")
        self.assertContains(r, "aria-label=\"completed today's win\"")
        # Every completed render offers the same prompt + footer.
        self.assertContains(r, 'class="win-again" id="win-again">Want another one?')
        self.assertContains(r, 'class="card-foot" id="win-foot-again"')
        self.assertContains(r, 'class="card-foot is-hidden" id="win-goal-foot"')

    def test_mark_complete_appears_on_todays_win_only_after_all_goal_steps(self):
        self.p.onboarded_at = timezone.now()
        self.p.save(update_fields=["onboarded_at"])
        goal = create_north_star(self.p, "Launch portfolio", ["Write copy", "Publish"])
        first, second = list(goal.stones.order_by("order", "created_at"))
        self._post("/daily/win/select/", {"id": first.id})

        first_done = self._post("/daily/win/", {"id": first.id, "action": "did_it"})
        self.assertFalse(first_done.json()["completed_today"]["goal_can_complete"])
        before = self.c.get("/daily/checkin/")
        self.assertContains(before, 'class="card-foot is-hidden" id="win-goal-foot"')

        self._post("/daily/win/", {"id": second.id, "action": "check_off"})
        after = self.c.get("/daily/checkin/")
        self.assertContains(after, 'class="card-foot" id="win-goal-foot"')
        self.assertContains(after, f'data-goal-id="{goal.id}"')
        self.assertContains(after, ">Mark complete</button>")

    def test_goal_summit_day_stars_week_strip(self):
        """North-star summit days star the strip — including wins completed
        before selection dates were retained (surfaced_on=None history)."""
        from django.utils import timezone as djtz

        self.p.onboarded_at = djtz.now()
        self.p.save(update_fields=["onboarded_at"])
        goal = create_north_star(self.p, "Star goal", ["only step"])
        complete_win(goal.stones.get())  # unsurfaced completion: surfaced_on=None
        complete_goal(goal)

        r = self.c.get("/daily/checkin/")
        self.assertContains(r, 'class="wstar"')

    def test_achieved_page_shows_goal_with_steps(self):
        goal = create_north_star(self.p, "Run a 5k", ["Buy shoes", "Jog twice"])
        s1, s2 = list(goal.stones.order_by("order"))
        complete_win(s1)
        complete_win(s2)
        complete_goal(goal)

        # The editor has one history entry point; achieved and archived North
        # Stars share its single list, achieved rows dated "Achieved <date>".
        r = self.c.get("/daily/wins/")
        self.assertNotContains(r, "/daily/wins/achieved/")
        self.assertContains(r, "/daily/wins/archived/")
        self.assertNotContains(r, "Run a 5k")  # finished goals left the editor

        history = self.c.get("/daily/wins/archived/")
        self.assertContains(history, '<div class="eyebrow">Wins</div>', html=True, count=1)
        self.assertContains(history, "Run a 5k")
        self.assertContains(history, "Buy shoes")
        self.assertContains(history, "Move to Working Toward")
        self.assertContains(history, f'data-achieved-goal="{goal.id}"')
        self.assertContains(history, ">Achieved ")
        self.assertNotContains(history, 'data-archived-goal="')

        r2 = self.c.get("/daily/wins/achieved/")
        self.assertEqual(r2.status_code, 200)
        self.assertContains(r2, "Run a 5k")
        self.assertContains(r2, "Buy shoes")
        self.assertContains(r2, "Jog twice")
        self.assertContains(r2, "/daily/wins/")  # back link to the editor

        moved = self._post("/daily/win/goal/restore/", {"id": goal.id})
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(moved.json()["goal"]["text"], "Run a 5k")
        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_OPEN)
        self.assertIsNone(goal.done_at)
        self.assertEqual(
            list(goal.stones.order_by("order").values_list("status", flat=True)),
            [WinItem.STATUS_DONE, WinItem.STATUS_DONE],
        )

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
        self.assertContains(r, 'aria-label="Loading win history"')
        self.assertContains(r, '<div class="eyebrow">Wins</div>', html=True)
        self.assertContains(r, 'data-wins-loading-close')
        self.assertNotContains(r, "{# The full wins editor")
        self.assertContains(r, "/static/daily/images/jamie-avatar.jpg")
        self.assertContains(r, "Add to Home Screen")
        self.assertContains(r, "Turn on notifications")
        self.assertNotContains(r, "notification-nudge-bell")
