"""Regression tests for the pre-commit review fixes (see REVIEW_FINDINGS.md):
sub-item preservation through overnight mutations, evening-plan merge (reorder,
never shrink), the beta gate on beta onboarding, and crisis-regex precision."""
import json
from datetime import timedelta

from django.test import TestCase, Client
from django.utils import timezone

from daily.auth import SESSION_DAILY_PARTICIPANT_ID
from daily.models import (
    ChecklistVersion,
    CoachSuggestion,
    DailyCheckIn,
    DailyParticipant,
)
from daily.services.checklist import apply_pending_mutations


def _make_participant(**kwargs):
    return DailyParticipant.objects.create(
        display_name="Tester", kind=DailyParticipant.KIND_EXTERNAL, **kwargs
    )


class MutationSubItemMergeTests(TestCase):
    """apply_pending_mutations must re-attach existing sub-item drawers to
    preserved keys: the engine proposes bare {key,label} questions."""

    def setUp(self):
        self.p = _make_participant(beta=True, ai_mutations_enabled=True)
        self.today = timezone.localdate()
        self.version = ChecklistVersion.objects.create(
            participant=self.p,
            questions=[
                {"key": "q_gym", "label": "Gym", "items": [
                    {"key": "s_bench", "label": "Bench"},
                    {"key": "s_squat", "label": "Squat"},
                ]},
                {"key": "q_water", "label": "Drink water"},
            ],
            source=ChecklistVersion.SOURCE_BASELINE,
            is_current=True,
        )

    def _queue(self, proposed):
        ci = DailyCheckIn.objects.create(
            participant=self.p,
            date=self.today - timedelta(days=1),
            checklist_version=self.version,
        )
        return CoachSuggestion.objects.create(
            check_in=ci,
            suggestion_text="note",
            proposed_questions=proposed,
            status=CoachSuggestion.STATUS_PENDING,
        )

    def test_applied_mutation_preserves_subitems(self):
        self._queue([
            {"key": "q_gym", "label": "Gym"},          # kept, bare
            {"key": "q_new", "label": "Evening walk"}, # added
        ])
        promoted = apply_pending_mutations(self.p, self.today)
        self.assertEqual(promoted, 1)
        current = self.p.checklist_versions.get(is_current=True)
        by_key = {q["key"]: q for q in current.questions}
        self.assertEqual(
            [s["key"] for s in by_key["q_gym"]["items"]], ["s_bench", "s_squat"]
        )
        self.assertIn("q_new", by_key)
        self.assertNotIn("q_water", by_key)

    def test_identical_bare_proposal_is_noop(self):
        # Same keys/labels as current (sub-items omitted, as the engine emits):
        # after the merge it must compare EQUAL and stay a no-op, not churn a
        # version that would have dropped the drawer.
        sug = self._queue([
            {"key": "q_gym", "label": "Gym"},
            {"key": "q_water", "label": "Drink water"},
        ])
        promoted = apply_pending_mutations(self.p, self.today)
        self.assertEqual(promoted, 0)
        sug.refresh_from_db()
        self.assertEqual(sug.status, CoachSuggestion.STATUS_SHOWN)
        self.assertEqual(
            self.p.checklist_versions.get(is_current=True).id, self.version.id
        )


class EveningPlanMergeTests(TestCase):
    """Beta _queue_evening_plan reorders the list behind the 3 planned items;
    it must never shrink a user-curated list. Legacy keeps replace-with-3."""

    def _version(self, p, n=5):
        return ChecklistVersion.objects.create(
            participant=p,
            questions=[{"key": f"q_{i}", "label": f"Habit {i}"} for i in range(n)],
            source=ChecklistVersion.SOURCE_BASELINE,
            is_current=True,
        )

    def test_plan_keeps_unplanned_items(self):
        from daily.views import _queue_evening_plan

        p = _make_participant(beta=True, ai_mutations_enabled=True)
        self._version(p, 5)
        planned = [
            {"key": "q_frog", "label": "The frog"},
            {"key": "q_1", "label": "Habit 1"},
            {"key": "q_3", "label": "Habit 3"},
        ]
        sug, dropped = _queue_evening_plan(p, timezone.localdate(), planned, merge=True)
        self.assertIsNotNone(sug)
        self.assertEqual(dropped, [])
        keys = [q["key"] for q in sug.proposed_questions]
        # Planned first (frog leads), then the rest in existing order.
        self.assertEqual(keys, ["q_frog", "q_1", "q_3", "q_0", "q_2", "q_4"])

    def test_plan_rekeys_by_label_instead_of_duplicating(self):
        from daily.views import _queue_evening_plan

        p = _make_participant(beta=True, ai_mutations_enabled=True)
        self._version(p, 3)
        # The model re-invented keys for two existing habits (same labels).
        planned = [
            {"key": "new_a", "label": "  habit 2 "},   # normalizes to Habit 2
            {"key": "new_b", "label": "Habit 0"},
            {"key": "new_c", "label": "Brand new thing"},
        ]
        sug, dropped = _queue_evening_plan(p, timezone.localdate(), planned, merge=True)
        keys = [q["key"] for q in sug.proposed_questions]
        self.assertEqual(keys, ["q_2", "q_0", "new_c", "q_1"])
        self.assertEqual(dropped, [])

    def test_full_list_drops_new_items_never_habits(self):
        from daily.services.checklist import MAX_CHECKLIST_SIZE
        from daily.views import _queue_evening_plan

        p = _make_participant(beta=True, ai_mutations_enabled=True)
        self._version(p, MAX_CHECKLIST_SIZE)
        planned = [
            {"key": "q_5", "label": "Habit 5"},   # frog = existing, always fits
            {"key": "new_1", "label": "New one"},
            {"key": "new_2", "label": "New two"},
        ]
        sug, dropped = _queue_evening_plan(p, timezone.localdate(), planned, merge=True)
        self.assertIsNotNone(sug)
        self.assertEqual(sorted(dropped), ["New one", "New two"])
        keys = [q["key"] for q in sug.proposed_questions]
        self.assertEqual(len(keys), MAX_CHECKLIST_SIZE)  # nothing deleted
        self.assertEqual(keys[0], "q_5")                 # frog still leads
        self.assertEqual(set(keys), {f"q_{i}" for i in range(MAX_CHECKLIST_SIZE)})

    def test_full_list_with_new_frog_refuses(self):
        from daily.services.checklist import MAX_CHECKLIST_SIZE
        from daily.views import _queue_evening_plan

        p = _make_participant(beta=True, ai_mutations_enabled=True)
        self._version(p, MAX_CHECKLIST_SIZE)
        planned = [{"key": f"new_{i}", "label": f"New {i}"} for i in range(3)]
        sug, dropped = _queue_evening_plan(p, timezone.localdate(), planned, merge=True)
        self.assertIsNone(sug)
        self.assertEqual(dropped, ["New 0"])  # the frog that couldn't fit
        self.assertFalse(
            CoachSuggestion.objects.filter(check_in__participant=p).exists()
        )

    def test_legacy_plan_still_replaces_with_three(self):
        from daily.views import _queue_evening_plan

        p = _make_participant(beta=False)
        self._version(p, 3)
        planned = [{"key": f"n_{i}", "label": f"Item {i}"} for i in range(3)]
        sug, dropped = _queue_evening_plan(p, timezone.localdate(), planned, merge=False)
        self.assertEqual(dropped, [])
        # Frozen pre-beta behavior: exactly the 3 planned items, no merge.
        self.assertEqual(
            [q["key"] for q in sug.proposed_questions], ["n_0", "n_1", "n_2"]
        )


class SupportOnlyMutationTests(TestCase):
    """A beta support-only user must never see a note claiming a list change,
    and queued mutations must not survive to fire weeks later."""

    def test_pending_mutation_dismissed_on_render(self):
        p = _make_participant(beta=True, ai_mutations_enabled=False)
        p.onboarded_at = timezone.now()
        p.save(update_fields=["onboarded_at"])
        version = ChecklistVersion.objects.create(
            participant=p,
            questions=[{"key": "q_a", "label": "A"}],
            source=ChecklistVersion.SOURCE_BASELINE,
            is_current=True,
        )
        ci = DailyCheckIn.objects.create(
            participant=p,
            date=timezone.localdate() - timedelta(days=1),
            checklist_version=version,
        )
        sug = CoachSuggestion.objects.create(
            check_in=ci,
            suggestion_text='Your plan, set last night: "X" comes first.',
            proposed_questions=[{"key": "q_x", "label": "X"}],
            status=CoachSuggestion.STATUS_PENDING,
        )
        c = Client()
        s = c.session
        s[SESSION_DAILY_PARTICIPANT_ID] = p.id
        s.save()
        r = c.get("/daily/checkin/")
        self.assertEqual(r.status_code, 200)
        sug.refresh_from_db()
        self.assertEqual(sug.status, CoachSuggestion.STATUS_DISMISSED)
        # And the list was NOT rewritten.
        current = p.checklist_versions.get(is_current=True)
        self.assertEqual([q["key"] for q in current.questions], ["q_a"])


class OnboardingBetaGateTests(TestCase):
    def _client_for(self, participant):
        c = Client()
        s = c.session
        s[SESSION_DAILY_PARTICIPANT_ID] = participant.id
        s.save()
        return c

    def test_non_beta_user_rejected(self):
        p = _make_participant(beta=False)
        c = self._client_for(p)
        r = c.post(
            "/daily/onboarding/beta/",
            data=json.dumps({"label": "Walk", "focus": ""}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"], "not_beta")
        self.assertIsNone(p.checklist_versions.filter(is_current=True).first())

    def test_beta_user_allowed(self):
        p = _make_participant(beta=True)
        c = self._client_for(p)
        r = c.post(
            "/daily/onboarding/beta/",
            data=json.dumps({"label": "Walk", "focus": "health"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        version = p.checklist_versions.get(is_current=True)
        self.assertEqual([q["label"] for q in version.questions], ["Walk"])


class CrisisRegexTests(TestCase):
    def test_binge_requires_eating_or_drinking_context(self):
        from daily.services.ai_coach import detect_crisis

        # Media "binge" talk must NOT trip the vetted crisis response.
        self.assertFalse(detect_crisis("binged a whole show last night"))
        self.assertFalse(detect_crisis("we binge-watched the series"))
        # Real signals still must.
        self.assertTrue(detect_crisis("I've been binge eating again"))
        self.assertTrue(detect_crisis("binge-ate all weekend"))
        self.assertTrue(detect_crisis("binge drinking every night"))

    def test_gym_hyperbole_does_not_trip(self):
        from daily.services.ai_coach import detect_crisis

        self.assertFalse(detect_crisis("my legs are killing me after leg day"))
        self.assertFalse(detect_crisis("this diet is killing me"))
        self.assertFalse(detect_crisis("that workout hurt me more than expected"))
        # Real self-harm signals still trip.
        self.assertTrue(detect_crisis("I want to kill myself"))
        self.assertTrue(detect_crisis("I've been hurting myself"))
        self.assertTrue(detect_crisis("thinking about cutting my self"))
        # Third-party violence still trips via the abuse pattern.
        self.assertTrue(detect_crisis("he hits me when he's drunk"))
