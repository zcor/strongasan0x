"""Reversible global beta rollout command and new-account defaults."""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from daily.models import DailyBetaRollout, DailyParticipant
from daily.services.ai_coach import CHAT_SYSTEM_PROMPT_BETA


LEGACY_CONFIG = {"auto_bonus": True, "coach_note": True}


def _participant(name, **kwargs):
    kwargs.setdefault("beta", False)
    kwargs.setdefault("ai_mutations_enabled", False)
    kwargs.setdefault("focus", "")
    kwargs.setdefault("legacy_health_config", dict(LEGACY_CONFIG))
    kwargs.setdefault("onboarded_at", timezone.now())
    return DailyParticipant.objects.create(
        display_name=name,
        kind=DailyParticipant.KIND_EXTERNAL,
        **kwargs,
    )


class BetaDefaultTests(TestCase):
    def test_new_participants_default_to_beta(self):
        participant = DailyParticipant.objects.create(
            display_name="New user",
            kind=DailyParticipant.KIND_EXTERNAL,
        )

        self.assertIs(participant.beta, True)

    def test_mutation_enabled_beta_prompt_is_honest_about_wins(self):
        self.assertIn('"Add a win" in the Wins card', CHAT_SYSTEM_PROMPT_BETA)
        self.assertIn("You cannot create", CHAT_SYSTEM_PROMPT_BETA)
        self.assertIn("promise that a Win will appear later", CHAT_SYSTEM_PROMPT_BETA)


class BetaRolloutCommandTests(TestCase):
    def test_dry_run_changes_nothing(self):
        participant = _participant("Legacy")
        output = StringIO()

        call_command("set_beta", "--rollout", "--dry-run", stdout=output)

        participant.refresh_from_db()
        self.assertIs(participant.beta, False)
        self.assertFalse(DailyBetaRollout.objects.exists())
        self.assertIn("dry-run", output.getvalue())

    def test_canary_rollout_only_changes_requested_eligible_participants(self):
        canary = _participant("Canary")
        other = _participant("Other")
        existing_beta = _participant(
            "Existing beta", beta=True, focus=DailyParticipant.FOCUS_LIFE,
        )

        call_command(
            "set_beta", "--rollout", "--participant", str(canary.id),
            stdout=StringIO(),
        )

        canary.refresh_from_db()
        other.refresh_from_db()
        existing_beta.refresh_from_db()
        self.assertIs(canary.beta, True)
        self.assertIs(canary.ai_mutations_enabled, True)
        self.assertEqual(canary.focus, DailyParticipant.FOCUS_HEALTH)
        self.assertIs(other.beta, False)
        self.assertEqual(existing_beta.focus, DailyParticipant.FOCUS_LIFE)
        rollout = DailyBetaRollout.objects.get()
        self.assertEqual(rollout.target_count, 1)
        self.assertEqual(rollout.snapshot, [{
            "id": canary.id,
            "beta": False,
            "ai_mutations_enabled": False,
            "focus": "",
        }])

    def test_rollout_preserves_an_explicit_focus(self):
        participant = _participant("Life", focus=DailyParticipant.FOCUS_LIFE)

        call_command("set_beta", "--rollout", stdout=StringIO())

        participant.refresh_from_db()
        self.assertEqual(participant.focus, DailyParticipant.FOCUS_LIFE)
        self.assertIs(participant.ai_mutations_enabled, True)

    def test_rollout_keeps_naked_participant_support_only_until_onboarding(self):
        participant = _participant("Naked", onboarded_at=None)

        call_command("set_beta", "--rollout", stdout=StringIO())

        participant.refresh_from_db()
        self.assertIs(participant.beta, True)
        self.assertIs(participant.ai_mutations_enabled, False)
        self.assertEqual(participant.focus, "")

    def test_rollback_restores_exact_original_flags(self):
        first = _participant("First")
        second = _participant(
            "Second",
            ai_mutations_enabled=True,
            focus=DailyParticipant.FOCUS_LIFE,
        )
        call_command("set_beta", "--rollout", stdout=StringIO())
        rollout = DailyBetaRollout.objects.get()

        call_command(
            "set_beta", "--rollback", str(rollout.rollout_id),
            stdout=StringIO(),
        )

        first.refresh_from_db()
        second.refresh_from_db()
        rollout.refresh_from_db()
        self.assertEqual(
            (first.beta, first.ai_mutations_enabled, first.focus),
            (False, False, ""),
        )
        self.assertEqual(
            (second.beta, second.ai_mutations_enabled, second.focus),
            (False, True, DailyParticipant.FOCUS_LIFE),
        )
        self.assertEqual(rollout.status, DailyBetaRollout.STATUS_ROLLED_BACK)
        self.assertIsNotNone(rollout.rolled_back_at)

    def test_rollout_rejects_ineligible_requested_participant(self):
        participant = _participant("Already beta", beta=True)

        with self.assertRaises(CommandError):
            call_command(
                "set_beta", "--rollout", "--participant", str(participant.id),
                stdout=StringIO(),
            )

    def test_rollback_cannot_run_twice(self):
        _participant("Legacy")
        call_command("set_beta", "--rollout", stdout=StringIO())
        rollout = DailyBetaRollout.objects.get()
        call_command(
            "set_beta", "--rollback", str(rollout.rollout_id),
            stdout=StringIO(),
        )

        with self.assertRaises(CommandError):
            call_command(
                "set_beta", "--rollback", str(rollout.rollout_id),
                stdout=StringIO(),
            )
