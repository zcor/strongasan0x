"""Integration test for nested habit sub-items (add / toggle / derive / remove)."""
import json

from django.test import TestCase, Client

from daily.auth import SESSION_DAILY_PARTICIPANT_ID
from daily.models import (
    ChecklistVersion,
    DailyCheckIn,
    DailyCheckInAnswer,
    DailyParticipant,
)


class SubItemFlowTests(TestCase):
    def setUp(self):
        self.p = DailyParticipant.objects.create(
            display_name="Tester", kind=DailyParticipant.KIND_EXTERNAL
        )
        self.version = ChecklistVersion.objects.create(
            participant=self.p,
            questions=[
                {"key": "q_str", "label": "Strength training 30 min"},
                {"key": "q_water", "label": "Drink water"},
            ],
            source=ChecklistVersion.SOURCE_BASELINE,
            is_current=True,
        )
        self.c = Client()
        s = self.c.session
        s[SESSION_DAILY_PARTICIPANT_ID] = self.p.id
        s.save()

    def _post(self, url, body):
        return self.c.post(url, data=json.dumps(body), content_type="application/json")

    def _parent_state(self):
        ci = DailyCheckIn.objects.get(participant=self.p)
        return {a.question_key: a.state for a in ci.answers.all()}.get("q_str", "pending")

    def test_full_flow(self):
        # Add two sub-items under the strength habit.
        r1 = self._post("/daily/subitem/add/", {"parent_key": "q_str", "label": "Lateral raise"})
        self.assertEqual(r1.status_code, 200)
        s1 = r1.json()["item"]["key"]
        r2 = self._post("/daily/subitem/add/", {"parent_key": "q_str", "label": "Squat"})
        s2 = r2.json()["item"]["key"]

        v = ChecklistVersion.objects.get(id=self.version.id)
        q_str = next(q for q in v.questions if q["key"] == "q_str")
        self.assertEqual([s["label"] for s in q_str["items"]], ["Lateral raise", "Squat"])

        # Parent starts pending.
        self.assertEqual(self._parent_state(), "pending")

        # Check one sub-item -> parent auto-derives to done.
        r3 = self._post("/daily/item/", {"key": s1, "state": "done"})
        self.assertEqual(r3.json()["parent"], {"key": "q_str", "state": "done"})
        self.assertEqual(self._parent_state(), "done")
        # Ring counts the PARENT once, not the sub-items.
        self.assertEqual(r3.json()["done_count"], 1)

        # Uncheck it -> parent falls back to pending.
        r4 = self._post("/daily/item/", {"key": s1, "state": "pending"})
        self.assertEqual(r4.json()["parent"]["state"], "pending")
        self.assertEqual(self._parent_state(), "pending")

        # Check the other, then remove it -> parent re-derives to pending.
        self._post("/daily/item/", {"key": s2, "state": "done"})
        self.assertEqual(self._parent_state(), "done")
        r5 = self._post("/daily/subitem/remove/", {"parent_key": "q_str", "key": s2})
        self.assertEqual(r5.json()["parent"]["state"], "pending")
        self.assertEqual(self._parent_state(), "pending")
        # The removed sub-item's answer is gone.
        self.assertFalse(
            DailyCheckInAnswer.objects.filter(question_key=s2).exists()
        )

    def test_bad_parent_rejected(self):
        r = self._post("/daily/subitem/add/", {"parent_key": "nope", "label": "x"})
        self.assertEqual(r.status_code, 400)

    def test_batch_adds_multiple_subitems_in_one_request(self):
        response = self._post(
            "/daily/subitem/add/",
            {
                "parent_key": "q_str",
                "labels": ["Bench press", "Squat", "Deadlift", "Row", "Lunge"],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["label"] for item in response.json()["items"]],
            ["Bench press", "Squat", "Deadlift", "Row", "Lunge"],
        )
        parent = next(
            question
            for question in ChecklistVersion.objects.get(id=self.version.id).questions
            if question["key"] == "q_str"
        )
        self.assertEqual(
            [item["label"] for item in parent["items"]],
            ["Bench press", "Squat", "Deadlift", "Row", "Lunge"],
        )
        self.assertEqual(
            DailyCheckInAnswer.objects.filter(
                question_key__in=[item["key"] for item in parent["items"]]
            ).count(),
            5,
        )

    def test_small_step_change_rederives_an_earlier_direct_mark(self):
        # Once a habit gains small steps, those steps and its saved rule become
        # the source of truth, even if the parent was checked before they were
        # added.
        self._post("/daily/item/", {"key": "q_str", "state": "done"})
        r = self._post("/daily/subitem/add/", {"parent_key": "q_str", "label": "Bench"})
        sub = r.json()["item"]["key"]

        self._post("/daily/item/", {"key": sub, "state": "done"})
        r2 = self._post("/daily/item/", {"key": sub, "state": "pending"})
        self.assertEqual(r2.json()["parent"]["state"], "pending")
        self.assertEqual(self._parent_state(), "pending")

        r3 = self._post("/daily/subitem/remove/", {"parent_key": "q_str", "key": sub})
        self.assertEqual(r3.json()["parent"]["state"], "pending")

    def test_direct_parent_check_checks_every_small_step(self):
        self._post("/daily/subitem/add/", {"parent_key": "q_str", "label": "Bench"})
        self._post("/daily/subitem/add/", {"parent_key": "q_str", "label": "Squat"})
        parent = next(
            question
            for question in ChecklistVersion.objects.get(id=self.version.id).questions
            if question["key"] == "q_str"
        )

        response = self._post("/daily/item/", {"key": "q_str", "state": "done"})

        self.assertEqual(response.status_code, 200)
        check_in = DailyCheckIn.objects.get(participant=self.p)
        self.assertEqual(
            set(
                check_in.answers.filter(
                    question_key__in=[item["key"] for item in parent["items"]],
                    state=DailyCheckInAnswer.STATE_DONE,
                ).values_list("question_key", flat=True)
            ),
            {item["key"] for item in parent["items"]},
        )

    def test_all_steps_rule_waits_for_every_small_step(self):
        first = self._post(
            "/daily/subitem/add/",
            {"parent_key": "q_str", "label": "Bench press"},
        ).json()["item"]["key"]
        second = self._post(
            "/daily/subitem/add/",
            {"parent_key": "q_str", "label": "Squat"},
        ).json()["item"]["key"]
        edit = self._post(
            "/daily/item/edit/",
            {
                "key": "q_str",
                "label": "Strength training 30 min",
                "step_rule": "all",
            },
        )

        self.assertEqual(edit.status_code, 200)
        self.assertEqual(edit.json()["item"]["step_rule"], "all")

        one_done = self._post("/daily/item/", {"key": first, "state": "done"})
        self.assertEqual(one_done.json()["parent"]["state"], "pending")
        self.assertEqual(one_done.json()["done_count"], 0)

        both_done = self._post("/daily/item/", {"key": second, "state": "done"})
        self.assertEqual(both_done.json()["parent"]["state"], "done")
        self.assertEqual(both_done.json()["done_count"], 1)

        one_unchecked = self._post("/daily/item/", {"key": first, "state": "pending"})
        self.assertEqual(one_unchecked.json()["parent"]["state"], "pending")

    def test_changing_from_any_to_all_rederives_today(self):
        first = self._post(
            "/daily/subitem/add/",
            {"parent_key": "q_str", "label": "Bench press"},
        ).json()["item"]["key"]
        self._post(
            "/daily/subitem/add/",
            {"parent_key": "q_str", "label": "Squat"},
        )
        self._post("/daily/item/", {"key": first, "state": "done"})
        self.assertEqual(self._parent_state(), "done")

        response = self._post(
            "/daily/item/edit/",
            {
                "key": "q_str",
                "label": "Strength training 30 min",
                "step_rule": "all",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["state"], "pending")
        self.assertEqual(self._parent_state(), "pending")

    def test_edit_renames_small_steps_without_changing_keys_or_answers(self):
        first = self._post(
            "/daily/subitem/add/", {"parent_key": "q_str", "label": "Bench"}
        ).json()["item"]["key"]
        second = self._post(
            "/daily/subitem/add/", {"parent_key": "q_str", "label": "Squat"}
        ).json()["item"]["key"]
        self._post("/daily/item/", {"key": first, "state": "done"})

        response = self._post(
            "/daily/item/edit/",
            {
                "key": "q_str",
                "label": "Strength training 30 min",
                "items": [{"key": first, "label": "Bench press"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        items = response.json()["item"]["items"]
        self.assertEqual([item["label"] for item in items], ["Bench press", "Squat"])
        self.assertEqual([item["key"] for item in items], [first, second])
        ci = DailyCheckIn.objects.get(participant=self.p)
        self.assertEqual(ci.answers.get(question_key=first).state, "done")

    def test_edit_rejects_unknown_or_empty_step_renames(self):
        first = self._post(
            "/daily/subitem/add/", {"parent_key": "q_str", "label": "Bench"}
        ).json()["item"]["key"]

        unknown = self._post(
            "/daily/item/edit/",
            {
                "key": "q_str",
                "label": "Strength training 30 min",
                "items": [{"key": "nope", "label": "X"}],
            },
        )
        empty = self._post(
            "/daily/item/edit/",
            {
                "key": "q_str",
                "label": "Strength training 30 min",
                "items": [{"key": first, "label": "  "}],
            },
        )

        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(empty.status_code, 400)
        parent = next(
            question
            for question in ChecklistVersion.objects.get(id=self.version.id).questions
            if question["key"] == "q_str"
        )
        self.assertEqual([item["label"] for item in parent["items"]], ["Bench"])

    def test_direct_untap_clears_subitems(self):
        # Untapping the parent's check directly clears the whole habit for
        # today — a still-done sub must not re-derive it back to done.
        self._post("/daily/subitem/add/", {"parent_key": "q_str", "label": "Bench"})
        sub = ChecklistVersion.objects.get(id=self.version.id)
        sub_key = next(
            q for q in sub.questions if q["key"] == "q_str"
        )["items"][0]["key"]
        self._post("/daily/item/", {"key": sub_key, "state": "done"})
        self.assertEqual(self._parent_state(), "done")

        self._post("/daily/item/", {"key": "q_str", "state": "pending"})
        self.assertEqual(self._parent_state(), "pending")
        ci = DailyCheckIn.objects.get(participant=self.p)
        self.assertEqual(
            ci.answers.get(question_key=sub_key).state, "pending"
        )
