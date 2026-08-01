from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from datetime import date, timedelta
import json
import re
from rollcall.models import WeeklyRollCall, RollCallRanking


class Command(BaseCommand):
    help = 'Ingest roll call data for a weekly post on strongasan0x.com'

    def add_arguments(self, parser):
        parser.add_argument(
            '--week',
            type=str,
            help='Publication date in YYYY-MM-DD or MM-DD format (e.g., "2025-12-08" or "12-08"). If Monday, uses the previous week (that just ended). If non-Monday, calculates the Monday of that week and uses the previous week.'
        )
        parser.add_argument(
            '--week-start',
            type=str,
            help='Week start date (Monday) in YYYY-MM-DD format (alternative to --week)'
        )
        parser.add_argument(
            '--substack-url',
            type=str,
            required=True,
            help='Canonical on-site Roll Call URL (legacy option name)'
        )
        parser.add_argument(
            '--text-file',
            type=str,
            help='Path to file containing the full text of the post'
        )
        parser.add_argument(
            '--text',
            type=str,
            help='Full text of the post (alternative to --text-file)'
        )
        parser.add_argument(
            '--rankings-file',
            type=str,
            help='Path to JSON file containing rankings data'
        )
        parser.add_argument(
            '--rankings',
            type=str,
            help='JSON string containing rankings data (alternative to --rankings-file)'
        )
        parser.add_argument(
            '--overwrite',
            action='store_true',
            help='Overwrite existing roll call data if it exists'
        )
        parser.add_argument(
            '--publish',
            action='store_true',
            help='Set the roll call as published immediately upon ingestion'
        )

    def handle(self, *args, **options):
        # Determine week start date
        week_str = options.get('week')
        week_start_str = options.get('week_start')
        publish_flag = options.get('publish')
        
        if week_str and week_start_str:
            raise CommandError('Cannot specify both --week and --week-start. Use only one.')
        
        if not week_str and not week_start_str:
            raise CommandError('Either --week or --week-start must be provided.')
        
        if week_str:
            # Calculate week start (Monday) from publication date
            week_start = self._get_week_start_from_publication_date(week_str)
            week_end = week_start + timedelta(days=6)
            # Validate it's actually a Monday
            if week_start.weekday() != 0:
                raise CommandError('Calculated week start is not a Monday. This should not happen.')
            # Validate it's actually a Sunday
            if week_end.weekday() != 6:
                raise CommandError('Calculated week end is not a Sunday. This should not happen.')
            # Show helpful message
            given_date = self._parse_date(week_str)
            if given_date.weekday() == 0:  # Monday
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Publication date {week_str} (Monday) → Using previous week: {week_start} to {week_end}"
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Publication date {week_str} ({given_date.strftime('%A')}) → Using previous week: {week_start} to {week_end}"
                    )
                )
        else:
            # Parse week start date directly
            try:
                week_start = date.fromisoformat(week_start_str)
            except ValueError:
                raise CommandError(f'Invalid date format: {week_start_str}. Use YYYY-MM-DD format.')

            # Validate week_start is a Monday
            if week_start.weekday() != 0:
                raise CommandError(f'Week start date must be a Monday. {week_start.strftime("%A, %B %d, %Y")} is a {week_start.strftime("%A")}.')

            # Calculate week end date (Sunday)
            week_end = week_start + timedelta(days=6)
            if week_end.weekday() != 6:
                raise CommandError('Calculated week end date is not a Sunday. This should not happen.')

        # Get full text
        full_text = ""
        if options['text_file']:
            try:
                with open(options['text_file'], 'r', encoding='utf-8') as f:
                    full_text = f.read()
            except FileNotFoundError:
                raise CommandError(f'Text file not found: {options["text_file"]}')
            except Exception as e:
                raise CommandError(f'Error reading text file: {e}')
        elif options['text']:
            full_text = options['text']

        if not full_text:
            full_text = " "

        # Get rankings
        rankings_data = []
        if options['rankings_file']:
            try:
                with open(options['rankings_file'], 'r', encoding='utf-8') as f:
                    rankings_data = json.load(f)
            except FileNotFoundError:
                raise CommandError(f'Rankings file not found: {options["rankings_file"]}')
            except json.JSONDecodeError as e:
                raise CommandError(f'Invalid JSON in rankings file: {e}')
            except Exception as e:
                raise CommandError(f'Error reading rankings file: {e}')
        elif options['rankings']:
            try:
                rankings_data = json.loads(options['rankings'])
            except json.JSONDecodeError as e:
                raise CommandError(f'Invalid JSON in rankings string: {e}')

        # Validate rankings structure
        if not isinstance(rankings_data, list):
            raise CommandError('Rankings must be a list of objects.')
        
        if len(rankings_data) > 0 and len(rankings_data) != 10:
            self.stdout.write(self.style.WARNING(f'Warning: Expected 10 rankings, got {len(rankings_data)}'))

        # Validate rankings
        seen_ranks = set()
        for i, ranking in enumerate(rankings_data):
            if not isinstance(ranking, dict):
                raise CommandError(f'Ranking {i+1} must be a dictionary.')
            if 'rank' not in ranking:
                raise CommandError(f'Ranking {i+1} missing "rank" field.')
            if 'name' not in ranking:
                raise CommandError(f'Ranking {i+1} missing "name" field.')
            
            rank = ranking['rank']
            if not isinstance(rank, int) or rank < 1 or rank > 10:
                raise CommandError(f'Ranking {i+1} has invalid rank: {rank}. Must be 1-10.')
            if rank in seen_ranks:
                raise CommandError(f'Duplicate rank {rank} found.')
            seen_ranks.add(rank)

        # Check for existing roll call
        existing_roll_call = WeeklyRollCall.objects.filter(week_start_date=week_start).first()
        if existing_roll_call and not options['overwrite']:
            raise CommandError(
                f'Roll call for week of {week_start} already exists. '
                'Use --overwrite to replace it, or delete it first.'
            )

        # Create or update WeeklyRollCall
        substack_url = options['substack_url']
        
        if existing_roll_call and options['overwrite']:
            self.stdout.write(self.style.WARNING(f'Overwriting existing roll call for week of {week_start}'))
            existing_roll_call.week_end_date = week_end
            existing_roll_call.substack_url = substack_url
            existing_roll_call.full_text = full_text
            existing_roll_call.is_published = publish_flag # Set published status
            existing_roll_call.save()
            roll_call = existing_roll_call
            # Delete existing rankings
            RollCallRanking.objects.filter(weekly_roll_call=roll_call).delete()
            self.stdout.write(self.style.SUCCESS('Deleted existing rankings'))
        else:
            roll_call, created = WeeklyRollCall.objects.get_or_create(
                week_start_date=week_start,
                defaults={
                    'week_end_date': week_end,
                    'substack_url': substack_url,
                    'full_text': full_text,
                    'is_published': publish_flag # Set published status
                }
            )
            if created:
                self.stdout.write(self.style.SUCCESS(f'Created roll call for week of {week_start}'))
            else:
                self.stdout.write(f'Roll call for week of {week_start} already exists')

        # Validate the roll call
        try:
            roll_call.full_clean()
        except ValidationError as e:
            raise CommandError(f'Validation error: {e}')

        # Create rankings
        created_count = 0
        updated_count = 0
        
        for ranking_data in sorted(rankings_data, key=lambda x: x['rank']):
            rank = ranking_data['rank']
            name = ranking_data['name']
            twitter_handle = ranking_data.get('twitter_handle', '').strip()
            
            # Clean Twitter handle
            twitter_handle = self._clean_twitter_handle(twitter_handle)
            
            ranking, created = RollCallRanking.objects.get_or_create(
                weekly_roll_call=roll_call,
                rank=rank,
                defaults={
                    'name': name,
                    'twitter_handle': twitter_handle
                }
            )
            
            if not created:
                # Update existing ranking
                ranking.name = name
                ranking.twitter_handle = twitter_handle
                ranking.save()
                updated_count += 1
            else:
                created_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f'Successfully ingested roll call data!\n'
                f'  Week: {week_start} to {week_end}\n'
                f'  Created {created_count} rankings\n'
                f'  Updated {updated_count} rankings\n'
                f'  Total rankings: {created_count + updated_count}'
            )
        )

    def _parse_date(self, date_str: str) -> date:
        """
        Parse date string in YYYY-MM-DD or MM-DD format.
        If MM-DD format, assumes current year.
        
        Args:
            date_str: Date string in YYYY-MM-DD or MM-DD format
            
        Returns:
            date object
        """
        try:
            # Try full format first
            return date.fromisoformat(date_str)
        except ValueError:
            # Try MM-DD format (assume current year)
            try:
                parts = date_str.split('-')
                if len(parts) == 2:
                    month = int(parts[0])
                    day = int(parts[1])
                    # Use current year
                    from datetime import datetime
                    year = datetime.now().year
                    return date(year, month, day)
            except (ValueError, IndexError):
                pass
            raise CommandError(f'Invalid date format: {date_str}. Use YYYY-MM-DD or MM-DD format (e.g., "2025-12-08" or "12-08").')
    
    def _get_week_start_from_publication_date(self, date_str: str) -> date:
        """
        Get week start date (Monday) from publication date.
        
        If publication date is a Monday, returns the Monday of the previous week
        (the week that just ended). Otherwise, calculates the Monday of the week
        containing the date, then returns the previous week's Monday.
        
        Args:
            date_str: Publication date (typically a Monday) in YYYY-MM-DD or MM-DD format
        
        Returns:
            Monday date of the week that just ended
        """
        given_date = self._parse_date(date_str)
        weekday = given_date.weekday()
        
        # If it's Monday (weekday 0), the week that just ended is 7 days back
        if weekday == 0:
            # Previous week's Monday (7 days back)
            return given_date - timedelta(days=7)
        else:
            # Calculate the Monday of the week containing this date
            days_back_to_monday = weekday
            this_week_monday = given_date - timedelta(days=days_back_to_monday)
            # Then go back 7 days to get the previous week's Monday
            return this_week_monday - timedelta(days=7)
    
    def _clean_twitter_handle(self, handle):
        """Clean Twitter handle by removing @ and URL prefixes"""
        if not handle:
            return ''
        
        # Remove leading/trailing whitespace
        handle = handle.strip()
        
        # Remove @ symbol if present
        handle = handle.lstrip('@')
        
        # Remove URL prefixes
        handle = re.sub(r'^https?://(www\.)?(twitter\.com|x\.com)/', '', handle)
        handle = re.sub(r'^@', '', handle)
        
        return handle
