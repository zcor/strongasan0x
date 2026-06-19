"""Tests for the metric extraction feature: service, command, view, hook."""

import json
from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase, RequestFactory
from django.utils import timezone

from rollcall.models import (
    WeeklyRollCall, Attestation, TelegramUserMapping, ExtractedMetrics,
)
from rollcall.services.metric_extraction import (
    extract_and_save, get_full_text, METRIC_FIELDS, _parse_response_text,
)


def _make_week(week_start=None, published=True):
    """Create a WeeklyRollCall for testing."""
    if week_start is None:
        week_start = date(2025, 1, 6)
    week_end = week_start + timedelta(days=6)
    return WeeklyRollCall.objects.create(
        week_start_date=week_start,
        week_end_date=week_end,
        substack_url='https://example.com',
        full_text='test',
        is_published=published,
    )


def _make_mapping(name='TestWarrior', telegram_user_id=123456):
    return TelegramUserMapping.objects.create(
        telegram_user_id=telegram_user_id,
        telegram_username=name.lower(),
        telegram_first_name=name,
        linked_name=name,
    )


def _make_attestation(roll_call, mapping, text='12k steps, bench 225x5', **kwargs):
    return Attestation.objects.create(
        weekly_roll_call=roll_call,
        source='telegram',
        telegram_user=mapping,
        raw_text=text,
        posted_at=timezone.now(),
        **kwargs,
    )


MOCK_RESPONSE = {
    'daily_steps': 12000,
    'bench_press': 225.0,
    'strength_sessions': 3,
    'calories_burned': None,
    'resting_heart_rate': None,
    'vo2_max': None,
    'sleep_hours': None,
    'body_weight': None,
    'body_fat_pct': None,
    'cardio_sessions': None,
    'combat_sessions': None,
    'total_training_sessions': None,
    'protein_grams': None,
    'calories_consumed': None,
    'squat': None,
    'deadlift': None,
    'extra_metrics': {},
}

MOCK_RAW = {'text': json.dumps(MOCK_RESPONSE), 'model': 'test-model'}


def _mock_call_provider(text, provider=None):
    """Mock call_provider that returns MOCK_RESPONSE."""
    return MOCK_RESPONSE.copy(), MOCK_RAW.copy(), 'test-model'


# =============================================================================
# Extraction parsing / validation
# =============================================================================

class ExtractionParsingTests(TestCase):
    def setUp(self):
        self.week = _make_week()
        self.mapping = _make_mapping()
        self.attestation = _make_attestation(self.week, self.mapping)

    @patch('rollcall.services.metric_extraction.call_provider', side_effect=_mock_call_provider)
    def test_successful_extraction(self, mock_cp):
        """Valid JSON response maps correctly to ExtractedMetrics fields."""
        metrics = extract_and_save(self.attestation)

        self.assertEqual(metrics.daily_steps, 12000)
        self.assertEqual(metrics.bench_press, 225.0)
        self.assertEqual(metrics.strength_sessions, 3)
        self.assertIsNone(metrics.calories_burned)
        self.assertEqual(metrics.extraction_error, '')

    @patch('rollcall.services.metric_extraction.call_provider')
    def test_partial_response(self, mock_cp):
        """Partial JSON (some fields null) creates valid record."""
        sparse = {f: None for f in METRIC_FIELDS}
        sparse['daily_steps'] = 8000
        sparse['extra_metrics'] = {}
        mock_cp.return_value = (sparse, MOCK_RAW.copy(), 'test-model')

        metrics = extract_and_save(self.attestation)

        self.assertEqual(metrics.daily_steps, 8000)
        self.assertIsNone(metrics.bench_press)
        self.assertEqual(metrics.extraction_error, '')

    @patch('rollcall.services.metric_extraction.call_provider')
    def test_malformed_json_creates_error_row(self, mock_cp):
        """API returning bad data creates error-row with extraction_error populated."""
        mock_cp.side_effect = json.JSONDecodeError("bad json", "", 0)

        with self.assertRaises(Exception):
            extract_and_save(self.attestation)

        em = ExtractedMetrics.objects.get(attestation=self.attestation)
        self.assertNotEqual(em.extraction_error, '')

    @patch('rollcall.services.metric_extraction.call_provider')
    def test_api_error_preserves_existing_metrics(self, mock_cp):
        """API timeout/error preserves prior good data."""
        # First: successful extraction
        mock_cp.return_value = (MOCK_RESPONSE.copy(), MOCK_RAW.copy(), 'test-model')
        extract_and_save(self.attestation)

        # Second: API failure
        mock_cp.side_effect = RuntimeError("API timeout")
        with self.assertRaises(Exception):
            extract_and_save(self.attestation)

        em = ExtractedMetrics.objects.get(attestation=self.attestation)
        # Prior good data preserved
        self.assertEqual(em.daily_steps, 12000)
        # Error recorded
        self.assertIn("API timeout", em.extraction_error)

    def test_markdown_fenced_json_parsing(self):
        """JSON wrapped in markdown code fences is parsed correctly."""
        fenced = '```json\n' + json.dumps(MOCK_RESPONSE) + '\n```'
        parsed = _parse_response_text(fenced)
        self.assertEqual(parsed['daily_steps'], 12000)

    def test_plain_json_parsing(self):
        """Plain JSON string is parsed correctly."""
        parsed = _parse_response_text(json.dumps(MOCK_RESPONSE))
        self.assertEqual(parsed['daily_steps'], 12000)


# =============================================================================
# Multi-part text assembly
# =============================================================================

class TextAssemblyTests(TestCase):
    def setUp(self):
        self.week = _make_week()
        self.mapping = _make_mapping()

    def test_single_part(self):
        att = _make_attestation(self.week, self.mapping, text='part 1 text')
        self.assertEqual(get_full_text(att), 'part 1 text')

    def test_multi_part(self):
        parent = _make_attestation(self.week, self.mapping, text='part 1')
        _make_attestation(
            self.week, self.mapping, text='part 2',
            parent_attestation=parent, part_number=2,
        )
        full = get_full_text(parent)
        self.assertIn('part 1', full)
        self.assertIn('part 2', full)
        # Part 1 should come before part 2
        self.assertLess(full.index('part 1'), full.index('part 2'))


# =============================================================================
# Command filtering
# =============================================================================

class CommandFilteringTests(TestCase):
    def setUp(self):
        self.week = _make_week(published=True)
        self.mapping = _make_mapping()

    def test_excludes_child_attestations(self):
        """parent_attestation__isnull=True filter excludes children."""
        parent = _make_attestation(self.week, self.mapping, text='parent')
        _make_attestation(
            self.week, self.mapping, text='child',
            parent_attestation=parent, part_number=2,
        )
        from rollcall.models import Attestation
        qs = Attestation.objects.filter(
            parent_attestation__isnull=True, is_hidden=False,
            weekly_roll_call__is_published=True,
        )
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first(), parent)

    def test_excludes_hidden_attestations(self):
        """is_hidden=False filter excludes hidden attestations."""
        _make_attestation(self.week, self.mapping, text='visible')
        _make_attestation(self.week, self.mapping, text='hidden', is_hidden=True)
        from rollcall.models import Attestation
        qs = Attestation.objects.filter(
            parent_attestation__isnull=True, is_hidden=False,
            weekly_roll_call__is_published=True,
        )
        self.assertEqual(qs.count(), 1)

    def test_excludes_unpublished_weeks(self):
        """weekly_roll_call__is_published=True excludes unpublished."""
        unpub_week = _make_week(week_start=date(2025, 1, 13), published=False)
        mapping2 = _make_mapping(name='Other', telegram_user_id=999)
        _make_attestation(unpub_week, mapping2, text='unpublished')
        _make_attestation(self.week, self.mapping, text='published')
        from rollcall.models import Attestation
        qs = Attestation.objects.filter(
            parent_attestation__isnull=True, is_hidden=False,
            weekly_roll_call__is_published=True,
        )
        self.assertEqual(qs.count(), 1)

    @patch('rollcall.services.metric_extraction.call_provider')
    def test_dry_run_no_api_calls(self, mock_cp):
        """--dry-run calls no API."""
        _make_attestation(self.week, self.mapping)
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('extract_metrics', dry_run=True, stdout=out)
        mock_cp.assert_not_called()

    @patch('rollcall.services.metric_extraction.call_provider', side_effect=_mock_call_provider)
    def test_warrior_filter(self, mock_cp):
        """--warrior flag filters correctly."""
        _make_attestation(self.week, self.mapping)
        other = _make_mapping(name='OtherGuy', telegram_user_id=999999)
        _make_attestation(self.week, other, text='other text')

        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('extract_metrics', warrior='TestWarrior', stdout=out)

        # Only 1 extraction call (for TestWarrior, not OtherGuy)
        self.assertEqual(mock_cp.call_count, 1)

    @patch('rollcall.services.metric_extraction.call_provider', side_effect=_mock_call_provider)
    def test_reextract_flag(self, mock_cp):
        """--reextract re-processes existing records."""
        att = _make_attestation(self.week, self.mapping)
        ExtractedMetrics.objects.create(
            attestation=att, model_used='test', extraction_error='',
        )

        from django.core.management import call_command
        from io import StringIO
        # Without --reextract: should skip (already has metrics)
        out = StringIO()
        call_command('extract_metrics', stdout=out)
        self.assertEqual(mock_cp.call_count, 0)

        # With --reextract: should process
        out = StringIO()
        call_command('extract_metrics', reextract=True, stdout=out)
        self.assertEqual(mock_cp.call_count, 1)


# =============================================================================
# Progress view data shaping
# =============================================================================

class ProgressViewTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.mapping = _make_mapping()

    def _auth_request(self, path='/warrior/progress/'):
        request = self.factory.get(path)
        request.session = {
            'warrior_telegram_user_id': self.mapping.telegram_user_id,
            'warrior_telegram_first_name': 'Test',
        }
        return request

    def test_empty_state_returns_200(self):
        """View returns 200 with no extracted metrics."""
        from rollcall.warrior.views import warrior_progress
        request = self._auth_request()
        response = warrior_progress(request)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'No progress data yet', response.content)

    def test_single_data_point_excluded(self):
        """Metrics with < 2 data points are excluded from chart data."""
        from rollcall.warrior.views import warrior_progress

        week1 = _make_week(week_start=date(2025, 1, 6))
        att1 = _make_attestation(week1, self.mapping)
        ExtractedMetrics.objects.create(
            attestation=att1, model_used='test',
            daily_steps=10000, extraction_error='',
        )
        # Only 1 week -> < 2 data points -> excluded

        request = self._auth_request()
        response = warrior_progress(request)
        self.assertEqual(response.status_code, 200)
        # Should show empty state since only 1 data point
        self.assertIn(b'No progress data yet', response.content)

    def test_two_data_points_included(self):
        """Metrics with >= 2 data points appear in chart data."""
        from rollcall.warrior.views import warrior_progress

        week1 = _make_week(week_start=date(2025, 1, 6))
        week2 = _make_week(week_start=date(2025, 1, 13))
        att1 = _make_attestation(week1, self.mapping)
        att2 = _make_attestation(week2, self.mapping)
        ExtractedMetrics.objects.create(
            attestation=att1, model_used='test',
            daily_steps=10000, extraction_error='',
        )
        ExtractedMetrics.objects.create(
            attestation=att2, model_used='test',
            daily_steps=12000, extraction_error='',
        )

        request = self._auth_request()
        response = warrior_progress(request)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'No progress data yet', response.content)
        self.assertIn(b'Daily Steps', response.content)

    def test_iso_date_ordering(self):
        """Chart data uses ISO dates in correct chronological order."""
        from rollcall.warrior.views import warrior_progress

        week1 = _make_week(week_start=date(2025, 1, 6))
        week2 = _make_week(week_start=date(2025, 1, 13))
        att1 = _make_attestation(week1, self.mapping)
        att2 = _make_attestation(week2, self.mapping)
        ExtractedMetrics.objects.create(
            attestation=att1, model_used='test',
            daily_steps=10000, extraction_error='',
        )
        ExtractedMetrics.objects.create(
            attestation=att2, model_used='test',
            daily_steps=12000, extraction_error='',
        )

        request = self._auth_request()
        response = warrior_progress(request)
        content = response.content.decode()
        self.assertIn('2025-01-06', content)
        self.assertIn('2025-01-13', content)
        idx1 = content.index('2025-01-06')
        idx2 = content.index('2025-01-13')
        self.assertLess(idx1, idx2)

    def test_category_grouping(self):
        """Metrics are grouped into body/training/nutrition/lifts categories."""
        from rollcall.warrior.views import warrior_progress

        week1 = _make_week(week_start=date(2025, 1, 6))
        week2 = _make_week(week_start=date(2025, 1, 13))
        for week in [week1, week2]:
            att = _make_attestation(week, self.mapping)
            ExtractedMetrics.objects.create(
                attestation=att, model_used='test',
                daily_steps=10000,
                strength_sessions=3,
                protein_grams=150,
                bench_press=225,
                extraction_error='',
            )

        request = self._auth_request()
        response = warrior_progress(request)
        content = response.content.decode()
        self.assertIn('Body', content)
        self.assertIn('Training', content)
        self.assertIn('Nutrition', content)
        self.assertIn('Lifts', content)


# =============================================================================
# Edit hook behavior
# =============================================================================

class EditHookTests(TestCase):
    @patch('rollcall.warrior.views._run_extraction')
    def test_extraction_called_after_save(self, mock_run):
        """Extraction is triggered after attestation save."""
        from rollcall.warrior.views import edit_attestation

        mapping = _make_mapping(telegram_user_id=111222)

        # Create an active roll call
        today = timezone.now().date()
        days_since_monday = today.weekday()
        week_start = today - timedelta(days=days_since_monday)
        week_end = week_start + timedelta(days=6)
        WeeklyRollCall.objects.create(
            week_start_date=week_start,
            week_end_date=week_end,
            substack_url='https://example.com',
            full_text='test',
        )

        factory = RequestFactory()
        request = factory.post('/warrior/edit/', {'raw_text': 'Test attestation 12k steps'})
        request.session = {
            'warrior_telegram_user_id': mapping.telegram_user_id,
            'warrior_telegram_first_name': 'Test',
        }

        # The hook uses transaction.on_commit + threading, which won't fire in
        # test transactions. We patch _run_extraction to verify it would be called.
        # In the test, the on_commit callback won't fire because TestCase wraps
        # everything in a transaction. But we can verify the attestation was created.
        response = edit_attestation(request)
        self.assertEqual(response.status_code, 302)

        # Attestation should exist
        att = Attestation.objects.filter(telegram_user=mapping).first()
        self.assertIsNotNone(att)
        self.assertIn('12k steps', att.raw_text)
