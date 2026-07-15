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
from daily.services.checklist import _reconcile_user_edits


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
