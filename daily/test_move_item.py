"""Integration test for moving a standalone habit under another (item/move/)."""
import json

from django.test import TestCase, Client

from daily.auth import SESSION_DAILY_PARTICIPANT_ID
from daily.models import (
    ChecklistVersion,
    DailyCheckIn,
    DailyCheckInAnswer,
    DailyParticipant,
)


class MoveItemTests(TestCase):
    def setUp(self):
        self.p = DailyParticipant.objects.create(
            display_name="Tester", kind=DailyParticipant.KIND_EXTERNAL
        )
        self.version = ChecklistVersion.objects.create(
            participant=self.p,
            questions=[
                {"key": "q_gym", "label": "Go to the gym"},
                {"key": "q_bench", "label": "Bench press", "days": [0, 2, 4]},
                {"key": "q_water", "label": "Drink water",
                 "items": [{"key": "q_water_glass", "label": "One glass"}]},
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

    def _questions(self):
        return ChecklistVersion.objects.get(id=self.version.id).questions

    def test_move_single_into_single_makes_a_group(self):
        r = self._post("/daily/item/move/", {"key": "q_bench", "dest_key": "q_gym"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["moved_key"], "q_bench")
        self.assertEqual(data["dest_key"], "q_gym")

        qs = self._questions()
        keys = [q["key"] for q in qs]
        self.assertNotIn("q_bench", keys)  # lifted out of the top level
        gym = next(q for q in qs if q["key"] == "q_gym")
        # Moved habit keeps its stable key, becomes a step, drops its schedule.
        self.assertEqual(gym["items"], [{"key": "q_bench", "label": "Bench press"}])
        self.assertNotIn("days", gym["items"][0])

    def test_move_into_existing_group_appends(self):
        r = self._post("/daily/item/move/", {"key": "q_gym", "dest_key": "q_water"})
        self.assertEqual(r.status_code, 200)
        water = next(q for q in self._questions() if q["key"] == "q_water")
        self.assertEqual(
            [s["label"] for s in water["items"]], ["One glass", "Go to the gym"]
        )

    def test_moved_habit_answer_follows_and_derives_parent(self):
        # Mark the standalone habit done today, then move it into the gym.
        self._post("/daily/item/", {"key": "q_bench", "state": "done"})
        self._post("/daily/item/move/", {"key": "q_bench", "dest_key": "q_gym"})
        ci = DailyCheckIn.objects.get(participant=self.p)
        states = {a.question_key: a.state for a in ci.answers.all()}
        # The moved habit's done answer carries over on its stable key, so the
        # destination (default "any" rule) derives to done.
        self.assertEqual(states.get("q_bench"), DailyCheckInAnswer.STATE_DONE)
        self.assertEqual(states.get("q_gym"), DailyCheckInAnswer.STATE_DONE)

    def test_group_cannot_be_moved(self):
        r = self._post("/daily/item/move/", {"key": "q_water", "dest_key": "q_gym"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "has_steps")

    def test_bad_and_self_targets_rejected(self):
        self.assertEqual(
            self._post("/daily/item/move/", {"key": "q_gym", "dest_key": "q_gym"}).status_code,
            400,
        )
        self.assertEqual(
            self._post("/daily/item/move/", {"key": "nope", "dest_key": "q_gym"}).status_code,
            400,
        )
