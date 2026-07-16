"""Health-only Bonus behavior for the beta dashboard."""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from daily.auth import SESSION_DAILY_PARTICIPANT_ID
from daily.models import (
    ChecklistVersion,
    CoachSuggestion,
    DailyCheckIn,
    DailyCheckInAnswer,
    DailyParticipant,
)
from daily.services.ai_coach import _parse_response, generate_one_bonus
from daily.services.checklist import apply_pending_mutations


def _participant(*, beta):
    return DailyParticipant.objects.create(
        display_name="Tester",
        kind=DailyParticipant.KIND_EXTERNAL,
        beta=beta,
        ai_mutations_enabled=False,
        onboarded_at=timezone.now(),
    )


def _version(participant, *, bonus_questions):
    return ChecklistVersion.objects.create(
        participant=participant,
        questions=[
            {"key": "q_one", "label": "One"},
            {"key": "q_two", "label": "Two"},
            {"key": "q_three", "label": "Three"},
        ],
        bonus_questions=bonus_questions,
        source=ChecklistVersion.SOURCE_BASELINE,
        is_current=True,
    )


def _client_for(participant):
    client = Client()
    session = client.session
    session[SESSION_DAILY_PARTICIPANT_ID] = participant.id
    session.save()
    return client


class BonusRenderingTests(TestCase):
    @override_settings(DEBUG=False)
    def test_beta_dashboard_only_includes_explicitly_health_tagged_bonus(self):
        participant = _participant(beta=True)
        _version(participant, bonus_questions=[
            {
                "key": "bonus_water",
                "label": "Drank another glass of water",
                "category": "health",
            },
            {
                "key": "bonus_inbox",
                "label": "Cleared the inbox",
                "category": "productivity",
            },
            {"key": "bonus_old", "label": "Finished an old untagged bonus"},
        ])

        response = _client_for(participant).get("/daily/checkin/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["key"] for item in response.context["bonus_items"]],
            ["bonus_water"],
        )

    @override_settings(DEBUG=False)
    def test_legacy_dashboard_keeps_existing_bonus_behavior(self):
        participant = _participant(beta=False)
        _version(participant, bonus_questions=[
            {"key": "bonus_health", "label": "Went for a walk", "category": "health"},
            {"key": "bonus_admin", "label": "Cleared the inbox"},
        ])

        response = _client_for(participant).get("/daily/checkin/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["key"] for item in response.context["bonus_items"]],
            ["bonus_health", "bonus_admin"],
        )

    @override_settings(DEBUG=False)
    @patch("daily.views._generate_and_append_bonus")
    def test_beta_refill_ignores_hidden_untagged_bonus(self, generate_bonus):
        participant = _participant(beta=True)
        version = _version(
            participant,
            bonus_questions=[{"key": "bonus_old", "label": "Old untagged bonus"}],
        )
        check_in = DailyCheckIn.objects.create(
            participant=participant,
            date=timezone.localdate(),
            checklist_version=version,
        )
        for key in ("q_one", "q_two", "q_three"):
            DailyCheckInAnswer.objects.create(
                check_in=check_in,
                question_key=key,
                state=DailyCheckInAnswer.STATE_DONE,
            )
        generated = {
            "key": "bonus_sleep",
            "label": "Set a consistent bedtime",
            "category": "health",
        }
        generate_bonus.return_value = generated

        response = _client_for(participant).post(
            "/daily/bonus/next/",
            data=json.dumps({}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["new_bonus"], generated)
        self.assertTrue(generate_bonus.call_args.kwargs["health_only"])


class BonusGenerationTests(TestCase):
    def test_beta_overnight_parser_requires_and_preserves_health_category(self):
        raw = """Keep going.
```json
[{"key": "q_one", "label": "One"}]
```
```json
[{"key": "bonus_sleep", "label": "Set a bedtime", "category": "health"}]
```"""

        _note, _core, bonus = _parse_response(
            raw, expected_core_count=1, health_only_bonus=True,
        )

        self.assertEqual(bonus, [
            {"key": "bonus_sleep", "label": "Set a bedtime", "category": "health"}
        ])

    def test_beta_overnight_parser_rejects_non_health_bonus(self):
        raw = """Keep going.
```json
[{"key": "q_one", "label": "One"}]
```
```json
[{"key": "bonus_inbox", "label": "Cleared inbox", "category": "productivity"}]
```"""

        _note, _core, bonus = _parse_response(
            raw, expected_core_count=1, health_only_bonus=True,
        )

        self.assertIsNone(bonus)

    def test_legacy_overnight_parser_still_accepts_untagged_bonus(self):
        raw = """Keep going.
```json
[{"key": "q_one", "label": "One"}]
```
```json
[{"key": "bonus_anything", "label": "Did something useful"}]
```"""

        _note, _core, bonus = _parse_response(raw, expected_core_count=1)

        self.assertEqual(
            bonus,
            [{"key": "bonus_anything", "label": "Did something useful"}],
        )

    @patch("daily.services.ai_coach._get_llm_client")
    def test_live_beta_bonus_rejects_model_output_without_health_tag(self, get_client):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=(
                '{"key":"bonus_inbox","label":"Cleared inbox",'
                '"category":"productivity"}'
            )))],
        )
        client = MagicMock()
        client.chat.completions.create.return_value = response
        get_client.return_value = client

        item = generate_one_bonus(
            participant_name="Tester",
            attestation_text="",
            existing_items=[],
            health_only=True,
        )

        self.assertIsNone(item)
        prompt = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        self.assertIn('"category": "health"', prompt)


class BonusMutationTests(TestCase):
    def test_beta_apply_discards_queued_non_health_bonus(self):
        participant = _participant(beta=True)
        participant.ai_mutations_enabled = True
        participant.save(update_fields=["ai_mutations_enabled"])
        version = _version(participant, bonus_questions=[])
        check_in = DailyCheckIn.objects.create(
            participant=participant,
            date=timezone.localdate() - timedelta(days=1),
            checklist_version=version,
        )
        CoachSuggestion.objects.create(
            check_in=check_in,
            suggestion_text="A note",
            proposed_questions=list(version.questions),
            proposed_bonus=[
                {
                    "key": "bonus_walk",
                    "label": "Took a short walk",
                    "category": "health",
                },
                {
                    "key": "bonus_admin",
                    "label": "Cleared the inbox",
                    "category": "productivity",
                },
                {"key": "bonus_old", "label": "Old untagged bonus"},
            ],
            status=CoachSuggestion.STATUS_PENDING,
        )

        self.assertEqual(apply_pending_mutations(participant, timezone.localdate()), 1)
        current = participant.checklist_versions.get(is_current=True)
        self.assertEqual(current.bonus_questions, [
            {
                "key": "bonus_walk",
                "label": "Took a short walk",
                "category": "health",
            }
        ])
