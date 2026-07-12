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

    def test_direct_done_survives_sub_untap(self):
        # The user marks the habit done THEMSELVES, then adds a sub-item and
        # fiddles with it. Derivation may only undo its own writes: the
        # direct mark must survive a sub tap + untap (and sub removal).
        self._post("/daily/item/", {"key": "q_str", "state": "done"})
        r = self._post("/daily/subitem/add/", {"parent_key": "q_str", "label": "Bench"})
        sub = r.json()["item"]["key"]

        self._post("/daily/item/", {"key": sub, "state": "done"})
        r2 = self._post("/daily/item/", {"key": sub, "state": "pending"})
        self.assertEqual(r2.json()["parent"]["state"], "done")
        self.assertEqual(self._parent_state(), "done")

        r3 = self._post("/daily/subitem/remove/", {"parent_key": "q_str", "key": sub})
        self.assertEqual(r3.json()["parent"]["state"], "done")

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
