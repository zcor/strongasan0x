"""Integration coverage for adding, renaming, and deleting core habits."""

import json
from datetime import timedelta

from django.test import Client, TestCase
from django.utils import timezone

from daily.auth import SESSION_DAILY_PARTICIPANT_ID
from daily.models import (
    ChecklistVersion,
    DailyCheckIn,
    DailyCheckInAnswer,
    DailyParticipant,
)
from daily.services.checklist import _merge_subitems, _reconcile_user_edits


class HabitManagementTests(TestCase):
    def setUp(self):
        self.p = DailyParticipant.objects.create(
            display_name="Tester",
            kind=DailyParticipant.KIND_EXTERNAL,
            beta=True,
            onboarded_at=timezone.now(),
        )
        self.version = ChecklistVersion.objects.create(
            participant=self.p,
            questions=[
                {
                    "key": "u_strength",
                    "label": "Strenght training",
                    "items": [{"key": "u_bench", "label": "Bench press"}],
                },
                {"key": "q_water", "label": "Drink water"},
            ],
            source=ChecklistVersion.SOURCE_BASELINE,
            is_current=True,
        )
        self.client = Client()
        session = self.client.session
        session[SESSION_DAILY_PARTICIPANT_ID] = self.p.id
        session.save()

    def _post(self, url, body, query=""):
        return self.client.post(
            url + query,
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_custom_add_creates_one_pending_habit(self):
        response = self._post("/daily/item/add/", {"mode": "custom", "label": "Morning walk"})

        self.assertEqual(response.status_code, 200)
        item = response.json()["item"]
        self.assertEqual(item["label"], "Morning walk")
        self.assertTrue(item["key"].startswith("u_"))
        current = ChecklistVersion.objects.get(id=self.version.id)
        self.assertEqual([q["label"] for q in current.questions].count("Morning walk"), 1)
        self.assertEqual(
            DailyCheckInAnswer.objects.get(question_key=item["key"]).state,
            DailyCheckInAnswer.STATE_PENDING,
        )

    def test_view_only_day_rejects_habit_tap(self):
        # A habit tap on a day older than the 1-day edit window is refused, and
        # nothing is written.
        old = (timezone.localdate() - timedelta(days=3)).isoformat()
        r = self._post("/daily/item/", {"key": "q_water", "state": "done"}, query="?day=%s" % old)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "read_only_day")
        self.assertFalse(
            DailyCheckInAnswer.objects.filter(question_key="q_water", state="done").exists()
        )

    def test_yesterday_habit_tap_allowed(self):
        # Yesterday is inside the 1-day edit window: the tap goes through.
        yesterday = timezone.localdate() - timedelta(days=1)
        r = self._post("/daily/item/", {"key": "q_water", "state": "done"},
                       query="?day=%s" % yesterday.isoformat())
        self.assertEqual(r.status_code, 200)
        check_in = DailyCheckIn.objects.get(participant=self.p, date=yesterday)
        self.assertEqual(
            DailyCheckInAnswer.objects.get(check_in=check_in, question_key="q_water").state,
            "done",
        )

    def test_edit_preserves_key_state_and_nested_items(self):
        self._post("/daily/item/", {"key": "u_strength", "state": "done"})

        response = self._post(
            "/daily/item/edit/",
            {"key": "u_strength", "label": "Strength training"},
        )

        self.assertEqual(response.status_code, 200)
        current = ChecklistVersion.objects.get(id=self.version.id)
        edited = next(q for q in current.questions if q["key"] == "u_strength")
        self.assertEqual(edited["label"], "Strength training")
        self.assertEqual(edited["items"], [{"key": "u_bench", "label": "Bench press"}])
        self.assertEqual(
            DailyCheckInAnswer.objects.get(question_key="u_strength").state,
            DailyCheckInAnswer.STATE_DONE,
        )

    def test_delete_removes_habit_details_and_todays_answers(self):
        self._post("/daily/item/", {"key": "u_strength", "state": "done"})
        self._post("/daily/item/", {"key": "u_bench", "state": "done"})

        response = self._post("/daily/item/remove/", {"key": "u_strength"})

        self.assertEqual(response.status_code, 200)
        current = ChecklistVersion.objects.get(id=self.version.id)
        self.assertEqual(current.questions, [{"key": "q_water", "label": "Drink water"}])
        self.assertFalse(DailyCheckInAnswer.objects.filter(question_key__in=["u_strength", "u_bench"]).exists())

    def test_delete_keeps_older_answer_rows(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        old_checkin = DailyCheckIn.objects.create(
            participant=self.p,
            date=yesterday,
            checklist_version=self.version,
        )
        old_answer = DailyCheckInAnswer.objects.create(
            check_in=old_checkin,
            question_key="u_strength",
            state=DailyCheckInAnswer.STATE_DONE,
        )

        response = self._post("/daily/item/remove/", {"key": "u_strength"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(DailyCheckInAnswer.objects.filter(id=old_answer.id).exists())

    def test_edit_and_delete_reject_unknown_or_past_habits(self):
        bad_edit = self._post("/daily/item/edit/", {"key": "not_ours", "label": "Nope"})
        bad_delete = self._post("/daily/item/remove/", {"key": "not_ours"})
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        past_edit = self._post(
            "/daily/item/edit/",
            {"key": "u_strength", "label": "Nope"},
            query=f"?day={yesterday}",
        )
        past_delete = self._post(
            "/daily/item/remove/",
            {"key": "u_strength"},
            query=f"?day={yesterday}",
        )

        self.assertEqual(bad_edit.status_code, 400)
        self.assertEqual(bad_delete.status_code, 400)
        self.assertEqual(past_edit.json()["error"], "no_edit_in_backfill")
        self.assertEqual(past_delete.json()["error"], "no_edit_in_backfill")

    def test_beta_page_wires_loading_edit_and_delete_actions(self):
        response = self.client.get("/daily/checkin/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Adding…")
        self.assertContains(response, 'post("/daily/item/edit/"')
        self.assertContains(response, 'post("/daily/item/remove/"')
        self.assertContains(response, "window.elistSetExpanded = setExpanded;")
        self.assertContains(response, "data-elist-accordion")
        self.assertContains(response, "When is this habit complete?")
        self.assertContains(response, "Any step")
        self.assertContains(response, "All steps")
        self.assertContains(response, "window.elistSetExpanded(replacement, true);")
        self.assertContains(response, "if (event.target === addComposer) closeAdd();")
        self.assertNotContains(response, "focusName")
        self.assertNotContains(response, "name.select()")


class HabitSchedulingTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.today_weekday = self.today.weekday()
        self.other_weekday = (self.today_weekday + 1) % 7
        self.p = DailyParticipant.objects.create(
            display_name="Scheduler",
            kind=DailyParticipant.KIND_EXTERNAL,
            beta=True,
            onboarded_at=timezone.now(),
        )
        self.version = ChecklistVersion.objects.create(
            participant=self.p,
            questions=[
                {"key": "q_every", "label": "Every day"},
                {"key": "q_today", "label": "Today only", "days": [self.today_weekday]},
                {"key": "q_other", "label": "Another day", "days": [self.other_weekday]},
            ],
            source=ChecklistVersion.SOURCE_BASELINE,
            is_current=True,
        )
        self.client = Client()
        session = self.client.session
        session[SESSION_DAILY_PARTICIPANT_ID] = self.p.id
        session.save()

    def _post(self, url, body):
        return self.client.post(
            url,
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_today_shows_only_habits_scheduled_for_today(self):
        response = self.client.get("/daily/checkin/")

        self.assertContains(response, "Every day")
        self.assertContains(response, "Today only")
        self.assertNotContains(response, "Another day")
        self.assertContains(response, "Your habits ›")
        self.assertNotContains(response, 'id="habit-count"')
        self.assertNotContains(response, 'id="add-item-btn"')

    def test_edit_updates_name_and_weekday_schedule(self):
        response = self._post(
            "/daily/item/edit/",
            {"key": "q_every", "label": "Weekends", "days": [5, 6]},
        )

        self.assertEqual(response.status_code, 200)
        edited = next(
            question for question in self.version.__class__.objects.get(id=self.version.id).questions
            if question["key"] == "q_every"
        )
        self.assertEqual(edited["label"], "Weekends")
        self.assertEqual(edited["days"], [5, 6])
        self.assertEqual(response.json()["item"]["days"], [5, 6])

    def test_add_accepts_a_weekday_schedule(self):
        response = self._post(
            "/daily/item/add/",
            {"mode": "custom", "label": "Monday planning", "days": [0]},
        )

        self.assertEqual(response.status_code, 200)
        item = response.json()["item"]
        stored = next(
            question for question in ChecklistVersion.objects.get(id=self.version.id).questions
            if question["key"] == item["key"]
        )
        self.assertEqual(stored["days"], [0])

    def test_add_can_create_small_steps_with_the_habit(self):
        response = self._post(
            "/daily/item/add/",
            {
                "mode": "custom",
                "label": "Strength training",
                "days": [0, 2, 4],
                "steps": ["Bench press", "Squat"],
                "step_rule": "all",
            },
        )

        self.assertEqual(response.status_code, 200)
        item = response.json()["item"]
        self.assertEqual(
            [step["label"] for step in item["items"]],
            ["Bench press", "Squat"],
        )
        self.assertEqual(item["step_rule"], "all")
        stored = next(
            question for question in ChecklistVersion.objects.get(id=self.version.id).questions
            if question["key"] == item["key"]
        )
        self.assertEqual(stored["items"], [
            {"key": item["items"][0]["key"], "label": "Bench press"},
            {"key": item["items"][1]["key"], "label": "Squat"},
        ])
        self.assertEqual(stored["step_rule"], "all")
        answer_keys = set(
            DailyCheckInAnswer.objects.filter(
                question_key__in=[item["key"]] + [step["key"] for step in item["items"]]
            ).values_list("question_key", flat=True)
        )
        self.assertEqual(
            answer_keys,
            {item["key"], item["items"][0]["key"], item["items"][1]["key"]},
        )

    def test_schedule_requires_at_least_one_valid_weekday(self):
        empty = self._post(
            "/daily/item/edit/",
            {"key": "q_every", "label": "Every day", "days": []},
        )
        invalid = self._post(
            "/daily/item/edit/",
            {"key": "q_every", "label": "Every day", "days": [7]},
        )

        self.assertEqual(empty.status_code, 400)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(empty.json()["error"], "bad_days")

    def test_rejects_an_unknown_small_step_rule(self):
        response = self._post(
            "/daily/item/edit/",
            {"key": "q_every", "label": "Every day", "step_rule": "half"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "bad_step_rule")

    def test_habits_editor_fragment_contains_every_habit_and_schedule(self):
        response = self.client.get("/daily/habits/?fragment=1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-habits-editor')
        self.assertContains(response, 'class="habit-add-card esheet-card"')
        self.assertContains(response, 'class="habit-add-fields"')
        self.assertContains(response, "Add a habit")
        self.assertContains(response, "Every day")
        self.assertContains(response, "Another day")
        self.assertContains(response, '"days": [')
        self.assertContains(response, "When is this habit complete?")

    def test_schedule_survives_a_bare_coach_proposal(self):
        current = [
            {
                "key": "q_gym",
                "label": "Gym",
                "days": [0, 2, 4],
                "step_rule": "all",
                "items": [{"key": "s_press", "label": "Press"}],
            }
        ]

        merged = _merge_subitems(current, [{"key": "q_gym", "label": "Gym"}])

        self.assertEqual(merged[0]["days"], [0, 2, 4])
        self.assertEqual(merged[0]["items"], current[0]["items"])
        self.assertEqual(merged[0]["step_rule"], "all")


class HabitMutationReconciliationTests(TestCase):
    def test_user_rename_survives_an_older_queued_proposal(self):
        base = [{"key": "q_walk", "label": "Walk"}]
        current = [{"key": "q_walk", "label": "Morning walk"}]
        proposed = [
            {"key": "q_walk", "label": "Walk"},
            {"key": "q_read", "label": "Read"},
        ]

        reconciled = _reconcile_user_edits(base, current, proposed)

        self.assertEqual(reconciled[0], {"key": "q_walk", "label": "Morning walk"})
