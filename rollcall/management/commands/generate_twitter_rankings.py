from django.core.management.base import BaseCommand, CommandError
from datetime import date, datetime
from rollcall.models import WeeklyRollCall, RollCallRanking, DiscordUserMapping, TelegramUserMapping


class Command(BaseCommand):
    help = 'Generate Twitter rankings post with emoji prefixes and Twitter handles'

    def add_arguments(self, parser):
        parser.add_argument(
            '--week-start',
            type=str,
            help='Week start date (YYYY-MM-DD format, e.g., 2025-12-01). If not provided, uses most recent week.',
        )
        parser.add_argument(
            '--week',
            type=str,
            help='Publication date in YYYY-MM-DD or MM-DD format (e.g., "2025-12-08" or "12-08"). If Monday, uses the previous week.',
        )
        parser.add_argument(
            '--no-prose',
            action='store_true',
            help='Omit prose excerpt from tweet',
        )
        parser.add_argument(
            '--no-links',
            action='store_true',
            help='Omit links from tweet',
        )
        parser.add_argument(
            '--rankings-only',
            action='store_true',
            help='Output only rankings list (no date, prose, or links)',
        )
        parser.add_argument(
            '--output',
            type=str,
            help='Output file path (saves tweet text to file)',
        )
        parser.add_argument(
            '--post',
            action='store_true',
            help='Post the tweet thread to X via the API',
        )

    def handle(self, *args, **options):
        # Check for conflicting options
        if options.get('week') and options.get('week_start'):
            raise CommandError('Cannot specify both --week and --week-start. Use only one.')
        
        # Determine week start date
        if options.get('week'):
            week_start = self._get_week_start_from_publication_date(options['week'])
        elif options.get('week_start'):
            try:
                week_start = date.fromisoformat(options['week_start'])
            except ValueError:
                raise CommandError(f'Invalid date format: {options["week_start"]}. Use YYYY-MM-DD format.')
        else:
            # Default: most recent week
            roll_call = WeeklyRollCall.objects.order_by('-week_start_date').first()
            if not roll_call:
                raise CommandError('No roll call found. Please specify --week-start or --week.')
            week_start = roll_call.week_start_date

        # Get roll call
        roll_call = WeeklyRollCall.objects.filter(week_start_date=week_start).first()
        if not roll_call:
            raise CommandError(f'No roll call found for week starting {week_start}')

        # Get rankings
        rankings = RollCallRanking.objects.filter(
            weekly_roll_call=roll_call
        ).order_by('rank')

        if not rankings:
            raise CommandError(f'No rankings found for week starting {week_start}')

        # Medal emojis for top 3
        medal_emojis = {1: '🥇', 2: '🥈', 3: '🥉'}
        # Number emojis for ranks 4-10
        number_emojis = {4: '4️⃣', 5: '5️⃣', 6: '6️⃣', 7: '7️⃣', 8: '8️⃣', 9: '9️⃣', 10: '🔟'}

        # Generate Twitter post
        lines = []

        # Add header (unless rankings-only)
        if not options['rankings_only']:
            # Format date range: "Dec. 22 - 28, 2025" or "Mar. 30 - Apr. 5, 2026"
            start_month = roll_call.week_start_date.strftime('%b.')
            start_day = roll_call.week_start_date.day
            end_month = roll_call.week_end_date.strftime('%b.')
            end_day = roll_call.week_end_date.day
            year = roll_call.week_end_date.year
            if start_month == end_month:
                date_str = f"{start_month} {start_day} - {end_day}, {year}"
            else:
                date_str = f"{start_month} {start_day} - {end_month} {end_day}, {year}"

            lines.append("Are you Strong as an 0x?  🏋️🐂")
            lines.append("Ethereum's toughest warriors")
            lines.append(date_str)
            lines.append("")

        # Add rankings
        for ranking in rankings:
            # Get emoji for rank
            emoji = medal_emojis.get(ranking.rank) or number_emojis.get(ranking.rank, f"{ranking.rank}.")

            # Helper function to normalize names for matching
            def normalize_name_for_matching(name):
                """Normalize name by lowercasing and replacing underscores/spaces"""
                if not name:
                    return ""
                return name.lower().replace('_', ' ').replace('-', ' ').strip()
            
            # Try to get Twitter handle from mappings
            twitter_handle = None
            
            # Check RollCallRanking.twitter_handle first
            if ranking.twitter_handle:
                twitter_handle = ranking.twitter_handle.strip().lstrip('@')
            else:
                # Normalize ranking name for matching
                normalized_ranking_name = normalize_name_for_matching(ranking.name)
                
                # Try Discord mapping - exact match first
                discord_mapping = DiscordUserMapping.objects.filter(
                    linked_name=ranking.name,
                    is_active=True
                ).first()
                
                # If no exact match, try case-insensitive and normalized matching
                if not discord_mapping:
                    all_discord = DiscordUserMapping.objects.filter(is_active=True)
                    for mapping in all_discord:
                        normalized_mapping_name = normalize_name_for_matching(mapping.linked_name)
                        if normalized_mapping_name == normalized_ranking_name:
                            discord_mapping = mapping
                            break
                
                if discord_mapping and discord_mapping.linked_twitter_handle:
                    twitter_handle = discord_mapping.linked_twitter_handle.strip().lstrip('@')
                else:
                    # Try Telegram mapping - exact match first
                    telegram_mapping = TelegramUserMapping.objects.filter(
                        linked_name=ranking.name,
                        is_active=True
                    ).first()
                    
                    # If no exact match, try case-insensitive and normalized matching
                    if not telegram_mapping:
                        all_telegram = TelegramUserMapping.objects.filter(is_active=True)
                        for mapping in all_telegram:
                            normalized_mapping_name = normalize_name_for_matching(mapping.linked_name)
                            if normalized_mapping_name == normalized_ranking_name:
                                telegram_mapping = mapping
                                break
                    
                    if telegram_mapping and telegram_mapping.linked_twitter_handle:
                        twitter_handle = telegram_mapping.linked_twitter_handle.strip().lstrip('@')
            
            # Format the line (with 2-space indent)
            if twitter_handle:
                lines.append(f"  {emoji} @{twitter_handle}")
            else:
                # Fallback to name if no Twitter handle found
                self.stdout.write(
                    self.style.WARNING(f"⚠️  No Twitter handle found for {ranking.name} (rank {ranking.rank})")
                )
                lines.append(f"  {emoji} {ranking.name}")
        
        # Add links section (unless disabled or rankings-only)
        if not options['rankings_only'] and not options['no_links']:
            # The storage field is legacy. Roll Calls always live on-site.
            roll_call_link = (
                f"https://strongasan0x.com/roll-call/"
                f"{roll_call.week_end_date.isoformat()}/"
            )

            lines.append("")
            lines.append("Details & How to Join 👇")
            lines.append("")
            lines.append("Are you strong as an 0x?   We invite recruits to submit a weekly health attestation:  https://forms.gle/pwBvd15SmsjDPCfK7")
            lines.append("")
            lines.append("We feed it to the oracle and publish the top ten each week:")
            lines.append(roll_call_link)
            lines.append("")
            lines.append("MORE INFO")
            lines.append("Discord: https://discord.gg/2wQpAHme3R")
            lines.append("Website: https://strongasan0x.com")

        # Build the full post
        full_post = "\n".join(lines)

        # Output the Twitter post
        self.stdout.write("\n" + "="*60)
        self.stdout.write("TWITTER/X POST (Thread format):")
        self.stdout.write("="*60 + "\n")
        self.stdout.write(full_post)
        self.stdout.write("\n" + "="*60)

        # Calculate tweet 1 (rankings only) character count
        if not options['rankings_only'] and not options['no_links']:
            # Find where "Details & How to Join" starts
            rankings_end = full_post.find("\nDetails & How to Join")
            if rankings_end > 0:
                tweet1 = full_post[:rankings_end].rstrip()
                tweet2_onwards = full_post[rankings_end:].strip()
                self.stdout.write(f"Tweet 1 (Rankings): {len(tweet1)}/280 chars")
                if len(tweet1) <= 280:
                    self.stdout.write(self.style.SUCCESS(f"  ✅ Within limit"))
                else:
                    self.stdout.write(self.style.WARNING(f"  ⚠️ Over by {len(tweet1) - 280} chars"))
                self.stdout.write(f"Tweet 2+ (Details): {len(tweet2_onwards)} chars (copy as reply)")
            else:
                self.stdout.write(f"Total: {len(full_post)} chars")
        else:
            char_count = len(full_post)
            self.stdout.write(f"Character count: {char_count}/280")
            if char_count > 280:
                self.stdout.write(
                    self.style.WARNING(f"⚠️  Tweet exceeds 280 characters by {char_count - 280} characters!")
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(f"✅ Tweet is within limit ({280 - char_count} characters remaining)")
                )

        self.stdout.write("="*60 + "\n")

        # Save to file if requested
        if options.get('output'):
            with open(options['output'], 'w') as f:
                f.write(full_post)
            self.stdout.write(self.style.SUCCESS(f"Saved to: {options['output']}"))

        # Post to X if requested
        if options.get('post'):
            self._post_to_x(full_post)

    def _post_to_x(self, full_post):
        """Post tweet thread to X via API."""
        import tweepy
        from django.conf import settings as django_settings

        consumer_key = django_settings.X_CONSUMER_KEY
        consumer_secret = django_settings.X_CONSUMER_SECRET
        access_token = django_settings.X_ACCESS_TOKEN
        access_token_secret = django_settings.X_ACCESS_TOKEN_SECRET

        if not all([consumer_key, consumer_secret, access_token, access_token_secret]):
            self.stderr.write("X API credentials not configured in settings.")
            return

        client = tweepy.Client(
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            access_token=access_token,
            access_token_secret=access_token_secret,
        )

        # Split into tweet 1 (rankings) and tweet 2 (details)
        split_point = full_post.find("\nDetails & How to Join")
        if split_point > 0:
            tweet1_text = full_post[:split_point].rstrip()
            tweet2_text = full_post[split_point:].strip()
        else:
            tweet1_text = full_post
            tweet2_text = None

        try:
            # Post tweet 1
            response1 = client.create_tweet(text=tweet1_text)
            tweet1_id = response1.data['id']
            self.stdout.write(self.style.SUCCESS(f"✅ Tweet 1 posted: https://x.com/i/status/{tweet1_id}"))

            # Post tweet 2 as reply
            if tweet2_text:
                response2 = client.create_tweet(text=tweet2_text, in_reply_to_tweet_id=tweet1_id)
                tweet2_id = response2.data['id']
                self.stdout.write(self.style.SUCCESS(f"✅ Tweet 2 posted: https://x.com/i/status/{tweet2_id}"))

        except tweepy.TweepyException as e:
            self.stderr.write(f"Error posting to X: {e}")

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
        from datetime import timedelta
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
