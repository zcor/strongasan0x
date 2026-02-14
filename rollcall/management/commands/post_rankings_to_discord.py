from django.core.management.base import BaseCommand
from django.conf import settings
import asyncio
import logging
import discord
from datetime import date, datetime, timedelta
from rollcall.models import WeeklyRollCall, RollCallRanking, DiscordUserMapping
from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)

# Rankings channel ID from our earlier scan
RANKINGS_CHANNEL_ID = 1442798909717090386


class Command(BaseCommand):
    help = 'Post historical rankings to Discord #rankings channel'

    def add_arguments(self, parser):
        parser.add_argument(
            '--week',
            type=str,
            help='Publication date in YYYY-MM-DD or MM-DD format (e.g., "2025-12-08" or "12-08"). If Monday, uses the previous week (that just ended).',
        )
        parser.add_argument(
            '--week-start',
            type=str,
            help='Week start date (Monday) in YYYY-MM-DD format (e.g., 2025-12-01). Alternative to --week.',
        )
        parser.add_argument(
            '--all',
            action='store_true',
            help='Post all historical rankings',
        )
        parser.add_argument(
            '--delete',
            action='store_true',
            help='Delete previous ranking posts for the specified week(s)',
        )

    def handle(self, *args, **options):
        # Check for conflicting options
        if options.get('week') and options.get('week_start'):
            from django.core.management.base import CommandError
            raise CommandError('Cannot specify both --week and --week-start. Use only one.')
        
        if not settings.DISCORD_BOT_TOKEN:
            self.stdout.write(
                self.style.ERROR('DISCORD_BOT_TOKEN not set in environment variables')
            )
            return
        
        if not settings.DISCORD_GUILD_ID:
            self.stdout.write(
                self.style.ERROR('DISCORD_GUILD_ID not set in environment variables')
            )
            return

        async def post_rankings():
            intents = discord.Intents.default()
            intents.guilds = True
            intents.members = True
            
            bot = discord.Client(intents=intents)
            ready_event = asyncio.Event()
            
            @bot.event
            async def on_ready():
                ready_event.set()
            
            try:
                self.stdout.write("Connecting to Discord...")
                bot_task = asyncio.create_task(bot.start(settings.DISCORD_BOT_TOKEN))
                
                try:
                    await asyncio.wait_for(ready_event.wait(), timeout=30.0)
                    self.stdout.write("Bot is ready!")
                except asyncio.TimeoutError:
                    bot_task.cancel()
                    self.stdout.write(self.style.ERROR("Timeout waiting for bot to connect."))
                    return
                
                guild_id = int(settings.DISCORD_GUILD_ID)
                guild = bot.get_guild(guild_id)
                
                if not guild:
                    try:
                        guild = await bot.fetch_guild(guild_id)
                    except discord.HTTPException as e:
                        self.stdout.write(self.style.ERROR(f"Could not fetch guild {guild_id}: {e}"))
                        return
                
                # Get rankings channel
                channel = bot.get_channel(RANKINGS_CHANNEL_ID)
                if not channel:
                    self.stdout.write(self.style.ERROR(f"Could not find rankings channel (ID: {RANKINGS_CHANNEL_ID})"))
                    return
                
                self.stdout.write(f"Found rankings channel: #{channel.name}")
                
                # Get roll calls to post
                def get_roll_calls():
                    if options.get('week'):
                        # Calculate week start from publication date
                        week_str = options['week']
                        week_start = self._get_week_start_from_publication_date(week_str)
                        roll_call = WeeklyRollCall.objects.filter(week_start_date=week_start).first()
                        return [roll_call] if roll_call else []
                    elif options.get('week_start'):
                        try:
                            week_date = datetime.strptime(options['week_start'], '%Y-%m-%d').date()
                            roll_call = WeeklyRollCall.objects.filter(week_start_date=week_date).first()
                            return [roll_call] if roll_call else []
                        except ValueError:
                            return []
                    elif options['all']:
                        return list(WeeklyRollCall.objects.all().order_by('week_start_date'))
                    else:
                        # Default: most recent week
                        return [WeeklyRollCall.objects.order_by('-week_start_date').first()]
                
                roll_calls = await sync_to_async(get_roll_calls)()
                roll_calls = [rc for rc in roll_calls if rc]  # Filter out None
                
                if not roll_calls:
                    self.stdout.write(self.style.ERROR("No roll calls found to post"))
                    return
                
                # Get all user mappings
                # Prefer real Discord users (not manual_import with fake IDs starting with 900000000000000000)
                def get_user_mappings():
                    mappings = {}
                    name_mappings = {}  # by linked_name
                    twitter_mappings = {}  # by linked_twitter_handle
                    for m in DiscordUserMapping.objects.filter(
                        is_active=True
                    ):
                        # Only use real Discord users (not manual_import fake IDs)
                        # Real Discord IDs are typically much smaller or don't start with 900000000000000000
                        if m.discord_user_id < 900000000000000000:
                            # Real user - use it (or overwrite if we already have a fake one)
                            if m.linked_name:
                                name_mappings[m.linked_name] = m
                            if m.linked_twitter_handle:
                                twitter_mappings[m.linked_twitter_handle.lower()] = m
                        else:
                            # Fake user but no real one yet - use it as fallback
                            if m.linked_name and m.linked_name not in name_mappings:
                                name_mappings[m.linked_name] = m
                            if m.linked_twitter_handle and m.linked_twitter_handle.lower() not in twitter_mappings:
                                twitter_mappings[m.linked_twitter_handle.lower()] = m
                    return name_mappings, twitter_mappings
                
                name_mappings, twitter_mappings = await sync_to_async(get_user_mappings)()
                
                for roll_call in roll_calls:
                    # Delete previous messages if requested
                    if options['delete']:
                        date_str = roll_call.week_start_date.strftime('%m/%d/%Y')
                        deleted_count = 0
                        try:
                            async for message in channel.history(limit=100):
                                if message.author == bot.user and date_str in message.content:
                                    try:
                                        await message.delete()
                                        deleted_count += 1
                                        self.stdout.write(f"Deleted message: {message.id}")
                                    except discord.Forbidden:
                                        self.stdout.write(
                                            self.style.WARNING(f"Could not delete message {message.id}")
                                        )
                                    except Exception as e:
                                        self.stdout.write(
                                            self.style.WARNING(f"Error deleting message {message.id}: {e}")
                                        )
                            if deleted_count > 0:
                                self.stdout.write(
                                    self.style.SUCCESS(f"✅ Deleted {deleted_count} message(s) for week of {roll_call.week_start_date}")
                                )
                            else:
                                self.stdout.write(
                                    self.style.WARNING(f"No messages found to delete for week of {roll_call.week_start_date}")
                                )
                        except Exception as e:
                            self.stdout.write(
                                self.style.ERROR(f"Error deleting messages: {e}")
                            )
                        
                        # If only --delete was specified, delete and continue to repost
                        # (don't return, continue to posting below)
                    
                    def get_rankings():
                        return list(RollCallRanking.objects.filter(
                            weekly_roll_call=roll_call
                        ).order_by('rank'))
                    
                    rankings = await sync_to_async(get_rankings)()
                    
                    if not rankings:
                        self.stdout.write(
                            self.style.WARNING(f"No rankings found for week of {roll_call.week_start_date}")
                        )
                        continue
                    
                    # Format the date nicely using publication date (Monday after week ends)
                    # Publication date = week_end_date + 1 day
                    publication_date = roll_call.week_end_date + timedelta(days=1)
                    month = publication_date.strftime('%B')
                    day = publication_date.day
                    year = publication_date.year
                    date_str = f"{month} {day}, {year}"
                    
                    # Medal emojis for top 3
                    medal_emojis = {1: '🥇', 2: '🥈', 3: '🥉'}
                    # Number emojis for ranks 4-10
                    number_emojis = {4: '4️⃣', 5: '5️⃣', 6: '6️⃣', 7: '7️⃣', 8: '8️⃣', 9: '9️⃣', 10: '🔟'}
                    
                    # Extract first line of prose
                    prose_line = None
                    if roll_call.full_text:
                        # Split by newlines and filter out empty lines
                        text_lines = [line.strip() for line in roll_call.full_text.split('\n') if line.strip()]
                        if text_lines:
                            prose_line = text_lines[0]
                    
                    # Build the message with Discord formatting
                    # 1. Date as header (make it prominent with bold and spacing)
                    lines = [f"**{date_str}**", ""]
                    
                    # 2. Medal/Number emojis and people
                    for ranking in rankings:
                        # Get emoji for rank
                        emoji = medal_emojis.get(ranking.rank) or number_emojis.get(ranking.rank, f"{ranking.rank}.")
                        
                        # Check if user is linked in Discord
                        # Try exact name match first, then case-insensitive, then partial match, then Twitter handle
                        mapping = name_mappings.get(ranking.name)
                        if not mapping:
                            # Try case-insensitive name lookup
                            for linked_name, m in name_mappings.items():
                                if linked_name.lower() == ranking.name.lower():
                                    mapping = m
                                    break
                        
                        if not mapping:
                            # Try partial match - check if ranking name contains linked_name or vice versa
                            ranking_lower = ranking.name.lower().replace('@', '').replace('_', '').replace(' ', '')
                            for linked_name, m in name_mappings.items():
                                linked_lower = linked_name.lower().replace('@', '').replace('_', '').replace(' ', '')
                                if linked_lower == ranking_lower or linked_lower in ranking_lower or ranking_lower in linked_lower:
                                    mapping = m
                                    break
                        
                        if not mapping and ranking.twitter_handle:
                            # Try Twitter handle match
                            twitter_key = ranking.twitter_handle.lower().lstrip('@')
                            mapping = twitter_mappings.get(twitter_key)
                        
                        if mapping:
                            # Get the Discord member to tag them
                            member = guild.get_member(mapping.discord_user_id)
                            if not member:
                                # Try fetching if not in cache
                                try:
                                    member = await guild.fetch_member(mapping.discord_user_id)
                                except:
                                    pass
                            
                            if member:
                                lines.append(f"{emoji} {member.mention}")
                            else:
                                # Fallback to name if member not found
                                lines.append(f"{emoji} {ranking.name}")
                        else:
                            # No Discord link, just use name
                            lines.append(f"{emoji} {ranking.name}")
                    
                    # 3. Block-quoted prose (single line if available)
                    if prose_line:
                        lines.append("")
                        lines.append(f"> {prose_line}")
                    
                    # 4. Links on same line with bullet separator and text labels
                    lines.append("")
                    # Use publication date for website link (website handles Monday dates as publication dates)
                    date_url = publication_date.strftime('%Y-%m-%d')
                    website_link = f"https://strongasan0x.com/?date={date_url}"
                    
                    # Generate Substack link using publication date (format: roll-call-november-3-2025)
                    month_name = publication_date.strftime('%B').lower()
                    day = publication_date.day
                    year = publication_date.year
                    substack_slug = f"roll-call-{month_name}-{day}-{year}"
                    substack_link = f"https://strongasan0x.substack.com/p/{substack_slug}"
                    
                    # Use Discord markdown link format: [text](url) with bullet separator
                    # Note: Discord links don't show previews when formatted this way
                    lines.append(f"[Website]({website_link}) • [Substack]({substack_link})")
                    
                    message = "\n".join(lines)
                    
                    try:
                        # Suppress link embeds/previews
                        await channel.send(message, suppress_embeds=True)
                        self.stdout.write(
                            self.style.SUCCESS(f"✅ Posted rankings for week of {roll_call.week_start_date}")
                        )
                    except discord.Forbidden:
                        self.stdout.write(
                            self.style.ERROR(f"❌ No permission to send message to #{channel.name}")
                        )
                    except Exception as e:
                        self.stdout.write(
                            self.style.ERROR(f"❌ Error posting rankings: {e}")
                        )
                
            finally:
                try:
                    await bot.close()
                except:
                    pass
                if 'bot_task' in locals() and not bot_task.done():
                    bot_task.cancel()
                    try:
                        await bot_task
                    except (asyncio.CancelledError, Exception):
                        pass

        try:
            asyncio.run(post_rankings())
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error: {e}'))
            logger.error(f'Error posting rankings: {e}', exc_info=True)
    
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
            from django.core.management.base import CommandError
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
