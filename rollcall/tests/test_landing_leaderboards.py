"""Tests for the landing page leaderboard computation."""

import math
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

from django.test import TestCase, RequestFactory

from rollcall.models import (
    WeeklyRollCall, RollCallRanking,
    DiscordUserMapping, TelegramUserMapping,
)
from rollcall.views import (
    _rank_to_points, _clean_name, _build_mapping_indices,
    _enrich_name, _compute_leaderboards, landing,
)


class RankToPointsTests(TestCase):
    """Test the points conversion: rank 1..10 -> 10..1, rank 11+ -> 0."""

    def test_rank_1_gives_10_points(self):
        self.assertEqual(_rank_to_points(1), 10)

    def test_rank_10_gives_1_point(self):
        self.assertEqual(_rank_to_points(10), 1)

    def test_rank_5_gives_6_points(self):
        self.assertEqual(_rank_to_points(5), 6)

    def test_rank_11_gives_0_points(self):
        self.assertEqual(_rank_to_points(11), 0)

    def test_rank_0_gives_0_points(self):
        self.assertEqual(_rank_to_points(0), 0)

    def test_rank_100_gives_0_points(self):
        self.assertEqual(_rank_to_points(100), 0)


class ChampionScoreFormulaTests(TestCase):
    """Test the champion_score = pts * log2(1 + weeks) / sqrt(weeks) formula."""

    def test_one_week_rank_1(self):
        # 10 * log2(2) / sqrt(1) = 10.0
        score = 10 * math.log2(1 + 1) / math.sqrt(1)
        self.assertAlmostEqual(score, 10.0, places=1)

    def test_dominant_performer_beats_consistent_mediocre(self):
        # Spencer-like: 110 pts, 13 weeks
        dominant = 110 * math.log2(1 + 13) / math.sqrt(13)
        # CurveCap-like: 108 pts, 15 weeks
        consistent = 108 * math.log2(1 + 15) / math.sqrt(15)
        self.assertGreater(dominant, consistent)

    def test_consistent_low_ranker_beats_flash_in_pan(self):
        # Alice-like: 31 pts, 13 weeks (avg rank ~8.6, always shows up)
        consistent = 31 * math.log2(1 + 13) / math.sqrt(13)
        # Jones-like: 15 pts, 3 weeks (avg rank 6, only 3 appearances)
        flash = 15 * math.log2(1 + 3) / math.sqrt(3)
        self.assertGreater(consistent, flash)

    def test_newcomer_rank_1_limited_by_few_weeks(self):
        # 1 week rank 1: 10 * log2(2) / sqrt(1) = 10.0
        newcomer = 10 * math.log2(1 + 1) / math.sqrt(1)
        # 10 weeks avg rank 4: 70 * log2(11) / sqrt(10) = 76.6
        veteran = 70 * math.log2(1 + 10) / math.sqrt(10)
        self.assertGreater(veteran, newcomer)


class CleanNameTests(TestCase):
    """Test the fuzzy name cleanup function."""

    def test_basic_cleanup(self):
        self.assertEqual(_clean_name('Warrior One'), 'warriorne')

    def test_strips_underscores_pipes_dashes(self):
        self.assertEqual(_clean_name('war_rior|one-two'), 'warrirnetw')

    def test_removes_of_and_house(self):
        self.assertEqual(_clean_name('Knight of House Stark'), 'knightstark')

    def test_empty_string(self):
        self.assertEqual(_clean_name(''), '')

    def test_none(self):
        self.assertEqual(_clean_name(None), '')


class MappingIndicesTests(TestCase):
    """Test building and using preloaded mapping indices."""

    def test_discord_wins_over_telegram(self):
        discord = MagicMock(spec=DiscordUserMapping)
        discord.linked_name = 'Warrior'
        discord.linked_twitter_handle = 'warrior_x'

        telegram = MagicMock(spec=TelegramUserMapping)
        telegram.linked_name = 'Warrior'
        telegram.linked_twitter_handle = 'warrior_tg'

        exact, clean, twitter = _build_mapping_indices([discord], [telegram])

        # Discord should win for same name key
        self.assertEqual(exact['warrior'], discord)
        # Twitter indices both present under different keys
        self.assertEqual(twitter['warrior_x'], discord)
        self.assertEqual(twitter['warrior_tg'], telegram)

    def test_telegram_fills_gaps(self):
        telegram = MagicMock(spec=TelegramUserMapping)
        telegram.linked_name = 'UniqueWarrior'
        telegram.linked_twitter_handle = 'unique_tg'

        exact, clean, twitter = _build_mapping_indices([], [telegram])

        self.assertEqual(exact['uniquewarrior'], telegram)
        self.assertEqual(twitter['unique_tg'], telegram)


class EnrichNameTests(TestCase):
    """Test the name enrichment with identity precedence."""

    def setUp(self):
        self.discord = MagicMock(spec=DiscordUserMapping)
        self.discord.linked_name = 'DiscordWarrior'
        self.discord.linked_twitter_handle = 'disc_handle'

        self.exact = {'discordwarrior': self.discord}
        self.clean = {_clean_name('DiscordWarrior'): self.discord}
        self.twitter = {'disc_handle': self.discord}

    def test_twitter_match_highest_priority(self):
        display, handle = _enrich_name('SomeName', 'disc_handle', {}, {}, self.twitter)
        self.assertEqual(display, 'DiscordWarrior')
        self.assertEqual(handle, 'disc_handle')

    def test_exact_name_match(self):
        display, handle = _enrich_name('DiscordWarrior', None, self.exact, {}, {})
        self.assertEqual(display, 'DiscordWarrior')
        self.assertEqual(handle, 'disc_handle')

    def test_no_match_returns_original(self):
        display, handle = _enrich_name('Unknown', 'unknown_handle', {}, {}, {})
        self.assertEqual(display, 'Unknown')
        self.assertEqual(handle, 'unknown_handle')


class ChampionSortTests(TestCase):
    """Test champion leaderboard sort: higher score first, tiebreak by lower avg_rank."""

    def test_higher_score_first(self):
        entries = [
            {'champion_score': 50, 'avg_rank': 3.0},
            {'champion_score': 100, 'avg_rank': 5.0},
        ]
        sorted_entries = sorted(entries, key=lambda e: (-e['champion_score'], e['avg_rank']))
        self.assertEqual(sorted_entries[0]['champion_score'], 100)

    def test_tiebreak_by_avg_rank(self):
        entries = [
            {'champion_score': 100, 'avg_rank': 5.0},
            {'champion_score': 100, 'avg_rank': 2.0},
        ]
        sorted_entries = sorted(entries, key=lambda e: (-e['champion_score'], e['avg_rank']))
        self.assertAlmostEqual(sorted_entries[0]['avg_rank'], 2.0)


class ConsistencySortTests(TestCase):
    """Test consistency leaderboard sort: more weeks first, tiebreak by lower avg_rank."""

    def test_more_weeks_first(self):
        entries = [
            {'weeks_participated': 5, 'avg_rank': 3.0},
            {'weeks_participated': 10, 'avg_rank': 5.0},
        ]
        sorted_entries = sorted(entries, key=lambda e: (-e['weeks_participated'], e['avg_rank']))
        self.assertEqual(sorted_entries[0]['weeks_participated'], 10)

    def test_tiebreak_by_avg_rank(self):
        entries = [
            {'weeks_participated': 10, 'avg_rank': 5.0},
            {'weeks_participated': 10, 'avg_rank': 2.0},
        ]
        sorted_entries = sorted(entries, key=lambda e: (-e['weeks_participated'], e['avg_rank']))
        self.assertAlmostEqual(sorted_entries[0]['avg_rank'], 2.0)


class EmptyDataTests(TestCase):
    """Test that zero published weeks produce empty leaderboards without errors."""

    def test_no_published_weeks_empty_leaderboards(self):
        champions, consistency, total = _compute_leaderboards({}, {}, {})
        self.assertEqual(champions, [])
        self.assertEqual(consistency, [])
        self.assertEqual(total, 0)


class IntegrationLeaderboardTests(TestCase):
    """Integration tests with actual DB objects."""

    def setUp(self):
        # Create 3 weeks of published data
        self.weeks = []
        for i in range(3):
            monday = date(2025, 1, 6) + timedelta(weeks=i)
            sunday = monday + timedelta(days=6)
            week = WeeklyRollCall.objects.create(
                week_start_date=monday,
                week_end_date=sunday,
                substack_url=f'https://example.com/week{i+1}',
                full_text=f'Week {i+1} text',
                is_published=True,
            )
            self.weeks.append(week)

        # Warrior A: rank 1 all 3 weeks (30 pts, 3 weeks)
        # Warrior B: rank 2 weeks 1&2, rank 3 week 3 (17 pts, 3 weeks)
        # Warrior C: rank 1 week 3 only (10 pts, 1 week)
        for i, week in enumerate(self.weeks):
            if i < 2:
                RollCallRanking.objects.create(weekly_roll_call=week, rank=1, name='WarriorA')
                RollCallRanking.objects.create(weekly_roll_call=week, rank=2, name='WarriorB')
            else:
                RollCallRanking.objects.create(weekly_roll_call=week, rank=1, name='WarriorC')
                RollCallRanking.objects.create(weekly_roll_call=week, rank=2, name='WarriorA')
                RollCallRanking.objects.create(weekly_roll_call=week, rank=3, name='WarriorB')

    def test_champion_score_calculation(self):
        champions, _, total = _compute_leaderboards({}, {}, {})
        self.assertEqual(total, 3)

        # Build lookup by name
        by_name = {e['display_name']: e for e in champions}

        # WarriorA: 10+10+9=29 pts, 3 weeks -> 29 * log2(4) / sqrt(3)
        a = by_name['WarriorA']
        expected_a = 29 * math.log2(4) / math.sqrt(3)
        self.assertAlmostEqual(a['champion_score'], expected_a, places=1)

        # WarriorB: 9+9+8=26 pts, 3 weeks -> 26 * log2(4) / sqrt(3)
        b = by_name['WarriorB']
        expected_b = 26 * math.log2(4) / math.sqrt(3)
        self.assertAlmostEqual(b['champion_score'], expected_b, places=1)

        # WarriorC: 10 pts, 1 week -> 10 * log2(2) / sqrt(1) = 10.0
        c = by_name['WarriorC']
        self.assertAlmostEqual(c['champion_score'], 10.0, places=1)

    def test_champion_sort_order(self):
        champions, _, _ = _compute_leaderboards({}, {}, {})
        # A should be first, B second, C third
        self.assertEqual(champions[0]['display_name'], 'WarriorA')
        self.assertEqual(champions[1]['display_name'], 'WarriorB')
        self.assertEqual(champions[2]['display_name'], 'WarriorC')

    def test_consistency_sort_order(self):
        _, consistency, _ = _compute_leaderboards({}, {}, {})
        # A and B both have 3 weeks, C has 1
        self.assertEqual(consistency[0]['weeks_participated'], 3)
        self.assertEqual(consistency[1]['weeks_participated'], 3)
        self.assertEqual(consistency[2]['weeks_participated'], 1)
        # Tiebreak: A has lower avg_rank than B
        self.assertEqual(consistency[0]['display_name'], 'WarriorA')
        self.assertEqual(consistency[1]['display_name'], 'WarriorB')

    def test_name_normalization_groups_variants(self):
        """Variants like 'WarriorA' and 'warriora_manual_import' should group."""
        week = WeeklyRollCall.objects.create(
            week_start_date=date(2025, 2, 3),
            week_end_date=date(2025, 2, 9),
            substack_url='https://example.com/week4',
            full_text='Week 4',
            is_published=True,
        )
        RollCallRanking.objects.create(
            weekly_roll_call=week, rank=1, name='warriora_manual_import'
        )
        champions, _, _ = _compute_leaderboards({}, {}, {})
        # Should not create a separate entry for the variant
        names = [e['display_name'] for e in champions]
        # Both should merge under 'WarriorA' (the more common variant)
        self.assertIn('WarriorA', names)
        # The _manual_import variant should NOT appear separately
        self.assertNotIn('warriora_manual_import', names)

    def test_landing_view_returns_200(self):
        factory = RequestFactory()
        request = factory.get('/')
        response = landing(request)
        self.assertEqual(response.status_code, 200)

    def test_landing_view_empty_db(self):
        """Landing view works even with no data at all."""
        WeeklyRollCall.objects.all().delete()
        factory = RequestFactory()
        request = factory.get('/')
        response = landing(request)
        self.assertEqual(response.status_code, 200)
