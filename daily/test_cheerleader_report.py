"""Next-morning Jamie reports for the Life / support-only experience."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

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
from daily.services.ai_coach import generate_cheerleader_report
from daily.services.coach_runner import run_coach


def _life_participant():
    return DailyParticipant.objects.create(
        display_name="Tester",
        kind=DailyParticipant.KIND_EXTERNAL,
        beta=True,
        focus=DailyParticipant.FOCUS_LIFE,
        ai_mutations_enabled=False,
        onboarded_at=timezone.now(),
    )


def _prior_check_in(participant):
    day = timezone.localdate() - timedelta(days=1)
    weekday = day.weekday()
    version = ChecklistVersion.objects.create(
        participant=participant,
        questions=[
            {"key": "q_done", "label": "Sent the application", "days": [weekday]},
            {
                "key": "q_off_day",
                "label": "Only appears another day",
                "days": [(weekday + 1) % 7],
            },
        ],
        source=ChecklistVersion.SOURCE_BASELINE,
        is_current=True,
    )
    check_in = DailyCheckIn.objects.create(
        participant=participant,
        date=day,
        checklist_version=version,
    )
    DailyCheckInAnswer.objects.create(
        check_in=check_in,
        question_key="q_done",
        state=DailyCheckInAnswer.STATE_DONE,
    )
    return version, check_in


def _client_for(participant):
    client = Client()
    session = client.session
    session[SESSION_DAILY_PARTICIPANT_ID] = participant.id
    session.save()
    return client


class CheerleaderReportPromptTests(SimpleTestCase):
    @patch("daily.services.ai_coach._get_llm_client")
    def test_report_prompt_is_grounded_and_forbids_list_edits(self, get_client):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content="Yesterday: 1 of 2 habits done. You sent the application, nice work."
            ))],
        )
        client = MagicMock()
        client.chat.completions.create.return_value = response
        get_client.return_value = client
        context = {
            "participant_name": "Tester",
            "today": {
                "date": "2026-07-15",
                "answers": {
                    "Sent the application": "done",
                    "Took a bonus walk": "done",
                },
                "core_answers": {
                    "Sent the application": "done",
                    "Read for ten minutes": "untouched",
                },
                "comment": "Felt good to finally send it.",
            },
        }

        result = generate_cheerleader_report(
            context, completed_win_label="Booked the interview",
        )

        self.assertEqual(result[0], response.choices[0].message.content)
        messages = client.chat.completions.create.call_args.kwargs["messages"]
        system_prompt = messages[1]["content"]
        user_prompt = messages[-1]["content"]
        self.assertIn("Do not suggest new habits, edit the list", system_prompt)
        self.assertIn("Completed Today's Win: Booked the interview", user_prompt)
        self.assertIn("done: Sent the application", user_prompt)
        self.assertNotIn("Took a bonus walk", user_prompt)


class CheerleaderReportRunnerTests(TestCase):
    @patch("daily.services.coach_runner.refresh_coach_profile")
    @patch("daily.services.coach_runner.generate_suggestion")
    @patch("daily.services.coach_runner.generate_cheerleader_report")
    def test_life_run_saves_report_without_mutating_list(
        self, generate_report, generate_suggestion, refresh_profile,
    ):
        participant = _life_participant()
        version, check_in = _prior_check_in(participant)
        WinItem.objects.create(
            participant=participant,
            text="Booked the interview",
            status=WinItem.STATUS_DONE,
            surfaced_on=check_in.date,
            done_at=timezone.now(),
        )
        generate_report.return_value = (
            "Yesterday: 1 of 1 habits done. You sent the application and booked the interview!",
            "test-model",
            Decimal("0.000001"),
        )

        self.assertTrue(run_coach(check_in.id))

        report = CoachSuggestion.objects.get(check_in=check_in)
        self.assertEqual(report.rationale, CoachSuggestion.RATIONALE_DAILY_REPORT)
        self.assertIsNone(report.proposed_questions)
        self.assertIsNone(report.proposed_bonus)
        self.assertIsNone(report.base_questions)
        self.assertEqual(
            participant.checklist_versions.get(is_current=True).id,
            version.id,
        )
        context = generate_report.call_args.args[0]
        self.assertEqual(
            context["today"]["core_answers"],
            {"Sent the application": "done"},
        )
        self.assertEqual(
            generate_report.call_args.kwargs["completed_win_label"],
            "Booked the interview",
        )
        generate_suggestion.assert_not_called()
        refresh_profile.assert_not_called()

    @override_settings(DEBUG=False)
    @patch("daily.services.coach_runner.generate_cheerleader_report")
    def test_opening_app_lazily_prepares_one_time_report(self, generate_report):
        participant = _life_participant()
        _version, check_in = _prior_check_in(participant)
        generate_report.return_value = (
            "Yesterday: 1 of 1 habits done. You sent the application, nicely done.",
            "test-model",
            Decimal("0.000001"),
        )

        client = _client_for(participant)
        response = client.get("/daily/morning-report/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["report"]["text"],
            "Yesterday: 1 of 1 habits done. You sent the application, nicely done.",
        )
        report = CoachSuggestion.objects.get(check_in=check_in)
        self.assertEqual(report.rationale, CoachSuggestion.RATIONALE_DAILY_REPORT)
        self.assertEqual(report.status, CoachSuggestion.STATUS_SHOWN)
        self.assertEqual(CoachChatMessage.objects.filter(suggestion=report).count(), 1)
        self.assertEqual(client.get("/daily/chat/history/").json()["messages"], [])
