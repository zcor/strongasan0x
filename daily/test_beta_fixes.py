"""Regression tests for the pre-commit review fixes (see REVIEW_FINDINGS.md):
sub-item preservation through overnight mutations, evening-plan merge (reorder,
never shrink), the beta gate on beta onboarding, and crisis-regex precision.

Also covers the Climb beta-review fixes: skip-clears-subitem-done, the
case-folded label fallback in _merge_subitems, explicit North Star completion,
and the win_action 'promote' endpoint."""
import json
from datetime import timedelta

from django.db import connection
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from daily import views
from daily.auth import SESSION_DAILY_PARTICIPANT_ID
from daily.models import (
    ChecklistVersion,
    CoachChatMessage,
    CoachSuggestion,
    DailyCheckIn,
    DailyCheckInAnswer,
    DailyParticipant,
    WinItem,
)
from daily.services.checklist import apply_pending_mutations, dismiss_pending_mutations
from daily.services.wins import complete_win, create_north_star


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

    def test_pending_mutation_dismissed_when_support_mode_is_selected(self):
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
        self.assertEqual(dismiss_pending_mutations(p), 1)
        sug.refresh_from_db()
        self.assertEqual(sug.status, CoachSuggestion.STATUS_DISMISSED)
        # And the list was NOT rewritten.
        current = p.checklist_versions.get(is_current=True)
        self.assertEqual([q["key"] for q in current.questions], ["q_a"])


class DashboardQueryTests(TestCase):
    @override_settings(DEBUG=False)
    def test_support_only_dashboard_stays_within_eight_queries(self):
        participant = _make_participant(beta=True, ai_mutations_enabled=False)
        participant.onboarded_at = timezone.now()
        participant.save(update_fields=["onboarded_at"])
        version = ChecklistVersion.objects.create(
            participant=participant,
            questions=[{"key": "q_a", "label": "A"}],
            source=ChecklistVersion.SOURCE_BASELINE,
            is_current=True,
        )
        check_in = DailyCheckIn.objects.create(
            participant=participant,
            date=timezone.localdate() - timedelta(days=1),
            checklist_version=version,
            done_count=1,
        )
        DailyCheckInAnswer.objects.create(
            check_in=check_in,
            question_key="q_a",
            state=DailyCheckInAnswer.STATE_DONE,
        )
        CoachChatMessage.objects.create(
            participant=participant,
            role=CoachChatMessage.ROLE_COACH,
            text="This must wait until chat opens.",
            date=timezone.localdate(),
        )
        participant.streak_count = 1
        participant.streak_through_date = timezone.localdate() - timedelta(days=1)
        participant.save(update_fields=["streak_count", "streak_through_date"])
        request = RequestFactory().get("/daily/checkin/")
        request.session = {SESSION_DAILY_PARTICIPANT_ID: participant.id}

        with CaptureQueriesContext(connection) as queries:
            response = views.checkin(request)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "This must wait until chat opens.")
        self.assertFalse(any(
            "daily_coachchatmessage" in query["sql"].lower()
            for query in queries.captured_queries
        ))
        checkin_sql = next(
            query["sql"] for query in queries.captured_queries
            if 'FROM "daily_dailycheckin"' in query["sql"]
            and 'SELECT "daily_dailycheckin"."id"' in query["sql"]
        )
        self.assertIn(
            (timezone.localdate() - timedelta(days=6)).isoformat(),
            checkin_sql,
        )
        self.assertLessEqual(
            len(queries),
            8,
            msg="\n".join(query["sql"] for query in queries.captured_queries),
        )

    @override_settings(DEBUG=False)
    def test_lazy_wins_fragment_uses_two_queries(self):
        participant = _make_participant(beta=True, ai_mutations_enabled=False)
        create_north_star(participant, "Get a job", ["Update resume"])
        request = RequestFactory().get("/daily/wins/?fragment=1")
        request.session = {SESSION_DAILY_PARTICIPANT_ID: participant.id}

        with CaptureQueriesContext(connection) as queries:
            response = views.wins_edit(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Get a job")
        self.assertLessEqual(
            len(queries),
            2,
            msg="\n".join(query["sql"] for query in queries.captured_queries),
        )


class LazyChatHistoryTests(TestCase):
    def setUp(self):
        self.participant = _make_participant(beta=True, ai_mutations_enabled=False)
        self.participant.onboarded_at = timezone.now()
        self.participant.save(update_fields=["onboarded_at"])
        self.version = ChecklistVersion.objects.create(
            participant=self.participant,
            questions=[{"key": "q_a", "label": "A"}],
            source=ChecklistVersion.SOURCE_BASELINE,
            is_current=True,
        )
        self.client = Client()
        session = self.client.session
        session[SESSION_DAILY_PARTICIPANT_ID] = self.participant.id
        session.save()

    def test_history_is_returned_only_from_lazy_endpoint(self):
        CoachChatMessage.objects.create(
            participant=self.participant,
            role=CoachChatMessage.ROLE_USER,
            text="An earlier message",
            date=timezone.localdate(),
        )

        response = self.client.get("/daily/chat/history/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["messages"][0]["text"], "An earlier message")

    def test_morning_note_is_marked_and_seeded_when_chat_opens(self):
        check_in = DailyCheckIn.objects.create(
            participant=self.participant,
            date=timezone.localdate() - timedelta(days=1),
            checklist_version=self.version,
        )
        note = CoachSuggestion.objects.create(
            check_in=check_in,
            suggestion_text="A deferred morning note",
            status=CoachSuggestion.STATUS_PENDING,
        )

        dashboard = self.client.get("/daily/checkin/")
        self.assertEqual(dashboard.status_code, 200)
        note.refresh_from_db()
        self.assertEqual(note.status, CoachSuggestion.STATUS_PENDING)
        self.assertFalse(CoachChatMessage.objects.filter(suggestion=note).exists())

        response = self.client.get("/daily/chat/history/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["messages"][-1]["text"], "A deferred morning note")
        note.refresh_from_db()
        self.assertEqual(note.status, CoachSuggestion.STATUS_SHOWN)
        self.assertEqual(CoachChatMessage.objects.filter(suggestion=note).count(), 1)


class ProposalReconcileTests(TestCase):
    """A proposal generated last night must not revert instant edits the user
    made after it: apply-time reconciliation overlays additions/removals made
    since the snapshot in base_questions. Beta only; legacy applies as-is."""

    def _setup(self, beta=True, current=None):
        p = _make_participant(beta=beta, ai_mutations_enabled=True)
        version = ChecklistVersion.objects.create(
            participant=p,
            questions=current,
            source=ChecklistVersion.SOURCE_BASELINE,
            is_current=True,
        )
        ci = DailyCheckIn.objects.create(
            participant=p,
            date=timezone.localdate() - timedelta(days=1),
            checklist_version=version,
        )
        return p, version, ci

    def _suggest(self, ci, proposed, base):
        return CoachSuggestion.objects.create(
            check_in=ci, suggestion_text="note",
            proposed_questions=proposed, base_questions=base,
            status=CoachSuggestion.STATUS_PENDING,
        )

    def test_item_added_after_generation_survives(self):
        # Coach saw [a, b] and refined b; the user added c later that night.
        p, _v, ci = self._setup(current=[
            {"key": "a", "label": "A"},
            {"key": "b", "label": "B"},
            {"key": "c", "label": "C (added at 10pm)"},
        ])
        self._suggest(
            ci,
            proposed=[{"key": "a", "label": "A"}, {"key": "b", "label": "B refined"}],
            base=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
        )
        self.assertEqual(apply_pending_mutations(p, timezone.localdate()), 1)
        keys = [q["key"] for q in p.checklist_versions.get(is_current=True).questions]
        self.assertEqual(keys, ["a", "b", "c"])

    def test_unchanged_proposal_plus_user_add_is_noop(self):
        # Coach kept the list as-was; the user's later addition means the
        # reconciled proposal equals the current list: no version churn.
        p, v, ci = self._setup(current=[
            {"key": "a", "label": "A"},
            {"key": "c", "label": "C"},
        ])
        sug = self._suggest(
            ci,
            proposed=[{"key": "a", "label": "A"}],
            base=[{"key": "a", "label": "A"}],
        )
        self.assertEqual(apply_pending_mutations(p, timezone.localdate()), 0)
        sug.refresh_from_db()
        self.assertEqual(sug.status, CoachSuggestion.STATUS_SHOWN)
        self.assertEqual(p.checklist_versions.get(is_current=True).id, v.id)

    def test_item_swapped_away_after_generation_stays_gone(self):
        # Coach saw [a, b]; the user swapped b for d before morning. The
        # proposal's b must not resurrect; d must survive.
        p, _v, ci = self._setup(current=[
            {"key": "a", "label": "A"},
            {"key": "d", "label": "D (swapped in)"},
        ])
        self._suggest(
            ci,
            proposed=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
            base=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
        )
        self.assertEqual(apply_pending_mutations(p, timezone.localdate()), 0)
        keys = [q["key"] for q in p.checklist_versions.get(is_current=True).questions]
        self.assertEqual(keys, ["a", "d"])

    def test_legacy_participant_applies_proposal_as_is(self):
        # Frozen path: no reconciliation, the proposal lands verbatim.
        p, _v, ci = self._setup(beta=False, current=[
            {"key": "a", "label": "A"},
            {"key": "c", "label": "C"},
        ])
        self._suggest(
            ci,
            proposed=[{"key": "a", "label": "A refined"}],
            base=[{"key": "a", "label": "A"}],
        )
        self.assertEqual(apply_pending_mutations(p, timezone.localdate()), 1)
        keys = [q["key"] for q in p.checklist_versions.get(is_current=True).questions]
        self.assertEqual(keys, ["a"])


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

    def test_focus_answer_picks_the_jamie(self):
        # "What's this mostly for?" decides the coach mode: health gets the
        # full engine (overnight notes + tune-ups), life stays support-only.
        for focus, expect_mutations in (("health", True), ("life", False), ("", False)):
            p = _make_participant(beta=True)
            c = self._client_for(p)
            r = c.post(
                "/daily/onboarding/beta/",
                data=json.dumps({"label": "Walk", "focus": focus}),
                content_type="application/json",
            )
            self.assertEqual(r.status_code, 200)
            p.refresh_from_db()
            self.assertEqual(
                p.ai_mutations_enabled, expect_mutations,
                f"focus={focus!r} should set ai_mutations_enabled={expect_mutations}",
            )
            self.assertEqual(p.focus, focus)


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


class SkipClearsSubItemTests(TestCase):
    """set_item_state: SKIPPING a core habit with a done sub-item must clear
    that sub-item too, not just leave it stranded 'done' under a skipped
    parent (previously only the STATE_PENDING branch did this)."""

    def setUp(self):
        self.p = DailyParticipant.objects.create(
            display_name="Tester", kind=DailyParticipant.KIND_EXTERNAL
        )
        self.version = ChecklistVersion.objects.create(
            participant=self.p,
            questions=[
                {"key": "q_gym", "label": "Gym", "items": [
                    {"key": "s_bench", "label": "Bench"},
                    {"key": "s_squat", "label": "Squat"},
                ]},
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

    def _answer_state(self, key):
        ci = DailyCheckIn.objects.get(participant=self.p)
        return ci.answers.get(question_key=key).state

    def test_skip_clears_done_subitem(self):
        r1 = self._post("/daily/item/", {"key": "s_bench", "state": "done"})
        self.assertEqual(r1.json()["parent"], {"key": "q_gym", "state": "done"})
        self.assertEqual(self._answer_state("q_gym"), "done")

        r2 = self._post("/daily/item/", {"key": "q_gym", "state": "skip"})
        self.assertEqual(r2.status_code, 200)
        # The sub-item's own answer must be cleared back to pending...
        self.assertEqual(self._answer_state("s_bench"), "pending")

        # ...so a subsequent render doesn't re-derive the parent back to done.
        r3 = self.c.get("/daily/checkin/")
        self.assertEqual(r3.status_code, 200)
        ci = DailyCheckIn.objects.get(participant=self.p)
        self.assertEqual(ci.answers.get(question_key="q_gym").state, "skip")

    def test_pending_still_clears_done_subitem(self):
        # Regression guard: the pre-existing STATE_PENDING-clears-subitems
        # behavior must still work after adding the SKIP branch.
        self._post("/daily/item/", {"key": "s_bench", "state": "done"})
        self.assertEqual(self._answer_state("q_gym"), "done")

        r = self._post("/daily/item/", {"key": "q_gym", "state": "pending"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._answer_state("s_bench"), "pending")
        self.assertEqual(self._answer_state("q_gym"), "pending")


class MergeSubitemsLabelFallbackTests(TestCase):
    """_merge_subitems (via apply_pending_mutations): when a proposed question
    renames the key but keeps the label, the sub-item drawer must still be
    re-attached by a case-folded label match."""

    def setUp(self):
        self.p = DailyParticipant.objects.create(
            display_name="Tester", kind=DailyParticipant.KIND_EXTERNAL,
            beta=True, ai_mutations_enabled=True,
        )
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

    def test_key_rename_reattaches_subitems_by_exact_label(self):
        self._queue([
            {"key": "q_gym2", "label": "Gym"},       # AI renamed the key, same label
            {"key": "q_water", "label": "Drink water"},
        ])
        promoted = apply_pending_mutations(self.p, self.today)
        self.assertEqual(promoted, 1)
        current = self.p.checklist_versions.get(is_current=True)
        by_key = {q["key"]: q for q in current.questions}
        self.assertNotIn("q_gym", by_key)
        self.assertIn("q_gym2", by_key)
        self.assertEqual(
            [s["key"] for s in by_key["q_gym2"]["items"]], ["s_bench", "s_squat"]
        )

    def test_key_rename_reattaches_subitems_by_casefolded_label(self):
        self._queue([
            {"key": "q_gym3", "label": "  gym "},    # casefold + whitespace variant
            {"key": "q_water", "label": "Drink water"},
        ])
        promoted = apply_pending_mutations(self.p, self.today)
        self.assertEqual(promoted, 1)
        current = self.p.checklist_versions.get(is_current=True)
        by_key = {q["key"]: q for q in current.questions}
        self.assertIn("q_gym3", by_key)
        self.assertEqual(
            [s["key"] for s in by_key["q_gym3"]["items"]], ["s_bench", "s_squat"]
        )

    def test_genuinely_new_habit_gets_no_items(self):
        self._queue([
            {"key": "q_gym", "label": "Gym"},
            {"key": "q_water", "label": "Drink water"},
            {"key": "q_new", "label": "Evening walk"},  # no key or label match
        ])
        promoted = apply_pending_mutations(self.p, self.today)
        self.assertEqual(promoted, 1)
        current = self.p.checklist_versions.get(is_current=True)
        by_key = {q["key"]: q for q in current.questions}
        self.assertIn("q_new", by_key)
        self.assertNotIn("items", by_key["q_new"])


class WinRemoveKeepsGoalOpenTests(TestCase):
    """Removing the last open step enables the explicit Complete control."""

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

    def test_remove_last_open_stone_with_progress_keeps_goal_open(self):
        goal = create_north_star(self.p, "Declutter", ["Closet", "Garage"])
        s1, s2 = list(goal.stones.order_by("order"))
        complete_win(s1)  # progress already made on this goal

        r = self._post("/daily/win/remove/", {"id": s2.id})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertNotIn("north_star_done", body)

        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_OPEN)
        completed = self._post("/daily/win/goal/complete/", {"id": goal.id})
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.json()["north_star_done"]["title"], "Declutter")

    def test_remove_last_stone_of_untouched_goal_leaves_it_open(self):
        goal = create_north_star(self.p, "Someday", ["Only step"])
        (stone,) = list(goal.stones.all())

        r = self._post("/daily/win/remove/", {"id": stone.id})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertNotIn("north_star_done", body)

        goal.refresh_from_db()
        self.assertEqual(goal.status, WinItem.STATUS_OPEN)


class WinPromoteEndpointTests(TestCase):
    """win_action action='promote': graduates a one-off win into a recurring
    habit on the current checklist. Reachable from the UI now; test the
    endpoint directly, including the checklist-full failure mode."""

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

    def test_promote_graduates_win_into_habit(self):
        from daily.services.wins import add_win

        win = add_win(self.p, "Stretch every morning")
        r = self._post("/daily/win/", {"id": win.id, "action": "promote"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["item"]["core"])

        win.refresh_from_db()
        self.assertEqual(win.status, WinItem.STATUS_GRADUATED)

        current = self.p.checklist_versions.get(is_current=True)
        labels = [q["label"] for q in current.questions]
        self.assertIn("Stretch every morning", labels)

    def test_promote_when_checklist_full_returns_409_and_does_not_graduate(self):
        from daily.services.checklist import MAX_CHECKLIST_SIZE
        from daily.services.wins import add_win

        ChecklistVersion.objects.create(
            participant=self.p,
            questions=[
                {"key": f"q_{i}", "label": f"Habit {i}"}
                for i in range(MAX_CHECKLIST_SIZE)
            ],
            source=ChecklistVersion.SOURCE_BASELINE,
            is_current=True,
        )
        win = add_win(self.p, "One more thing")
        r = self._post("/daily/win/", {"id": win.id, "action": "promote"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "checklist_full")

        win.refresh_from_db()
        self.assertEqual(win.status, WinItem.STATUS_OPEN)
