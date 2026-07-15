"""Regression coverage for persisted daily and participant streak rollups."""
import json
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from daily.auth import SESSION_DAILY_PARTICIPANT_ID
from daily.models import (
    ChecklistVersion,
    DailyCheckIn,
    DailyCheckInAnswer,
    DailyParticipant,
)
from daily.services.streaks import (
    calculate_streak_state,
    current_streak,
    refresh_streak_cache,
)


class StreakCacheServiceTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.participant = DailyParticipant.objects.create(
            display_name="Streak tester",
            kind=DailyParticipant.KIND_EXTERNAL,
            beta=True,
        )
        self.version = ChecklistVersion.objects.create(
            participant=self.participant,
            questions=[{"key": "q_a", "label": "A"}],
            source=ChecklistVersion.SOURCE_BASELINE,
            is_current=True,
        )

    def test_calculation_preserves_adaptive_bar_and_consecutive_days(self):
        counts = {
            self.today - timedelta(days=3): 3,
            self.today - timedelta(days=2): 2,
            self.today - timedelta(days=1): 2,
            self.today: 1,
        }

        state = calculate_streak_state(counts, self.today)

        self.assertEqual(state["streak_bar"], 2)
        self.assertEqual(state["streak_count"], 3)
        self.assertEqual(state["streak_through_date"], self.today - timedelta(days=1))

    def test_cached_read_needs_no_database_query_and_handles_midnight(self):
        for offset in range(3):
            DailyCheckIn.objects.create(
                participant=self.participant,
                date=self.today - timedelta(days=offset),
                checklist_version=self.version,
                done_count=1,
            )
        refresh_streak_cache(self.participant, today=self.today)

        with CaptureQueriesContext(connection) as queries:
            today_streak = current_streak(self.participant, self.today)
            tomorrow_streak = current_streak(
                self.participant, self.today + timedelta(days=1)
            )
            expired_streak = current_streak(
                self.participant, self.today + timedelta(days=2)
            )

        self.assertEqual(len(queries), 0)
        self.assertEqual(today_streak, 3)
        self.assertEqual(tomorrow_streak, 3)
        self.assertEqual(expired_streak, 0)

    def test_rebuild_command_repairs_rollups_from_source_answers(self):
        check_in = DailyCheckIn.objects.create(
            participant=self.participant,
            date=self.today,
            checklist_version=self.version,
        )
        DailyCheckInAnswer.objects.create(
            check_in=check_in,
            question_key="q_a",
            state=DailyCheckInAnswer.STATE_DONE,
        )

        call_command(
            "rebuild_streak_cache",
            participant_id=self.participant.id,
            stdout=StringIO(),
        )

        check_in.refresh_from_db()
        self.participant.refresh_from_db()
        self.assertEqual(check_in.done_count, 1)
        self.assertEqual(self.participant.streak_count, 1)
        self.assertEqual(self.participant.streak_through_date, self.today)


class StreakCacheEndpointTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.participant = DailyParticipant.objects.create(
            display_name="Streak endpoint tester",
            kind=DailyParticipant.KIND_EXTERNAL,
            beta=True,
            onboarded_at=timezone.now(),
        )
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

    def _set(self, state, day=None):
        query = f"?day={day.isoformat()}" if day else ""
        return self.client.post(
            "/daily/item/" + query,
            data=json.dumps({"key": "q_a", "state": state}),
            content_type="application/json",
        )

    def test_check_uncheck_and_backfill_refresh_cache(self):
        yesterday = self.today - timedelta(days=1)

        self.assertEqual(self._set("done").status_code, 200)
        self.assertEqual(self._set("done", yesterday).status_code, 200)
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.streak_count, 2)
        self.assertEqual(self.participant.streak_through_date, self.today)
        self.assertEqual(
            list(
                self.participant.checkins.order_by("date")
                .values_list("date", "done_count")
            ),
            [(yesterday, 1), (self.today, 1)],
        )

        self.assertEqual(self._set("pending", yesterday).status_code, 200)
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.streak_count, 1)
        self.assertEqual(self.participant.streak_through_date, self.today)
        self.assertEqual(
            self.participant.checkins.get(date=yesterday).done_count,
            0,
        )
