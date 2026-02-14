from django.core.management.base import BaseCommand
from django.conf import settings
from django.db.models import Q
from asgiref.sync import sync_to_async
from datetime import datetime, timedelta, timezone
import discord
import asyncio
import logging


from bot.models import DiscordChannel, DiscordMessage
from bot.discord_bot import DiscordIntelligenceBot

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Backfill Discord message history for channels'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=30,
            help='Number of days to look back (default: 30)',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            help='Maximum number of messages per channel (default: unlimited)',
        )
        parser.add_argument(
            '--channel-id',
            type=int,
            default=None,
            help='Specific channel ID to backfill (default: all channels)',
        )
        parser.add_argument(
            '--server-id',
            type=int,
            default=None,
            help='Specific server/guild ID to backfill (default: all servers)',
        )
        parser.add_argument(
            '--skip-existing',
            action='store_true',
            help='Skip channels that already have recent messages',
        )

    def handle(self, *args, **options):
        if not settings.DISCORD_BOT_TOKEN:
            self.stdout.write(
                self.style.ERROR('DISCORD_BOT_TOKEN not set in environment variables')
            )
            return

        days = options['days']
        limit = options['limit']
        channel_id = options['channel_id']
        server_id = options['server_id']
        skip_existing = options['skip_existing']

        self.stdout.write(
            self.style.SUCCESS(f'Starting message backfill (last {days} days)...')
        )

        # Run the async backfill
        asyncio.run(self.backfill_messages(days, limit, channel_id, server_id, skip_existing))

    async def backfill_messages(self, days, limit, channel_id, server_id, skip_existing):
        """Backfill messages from Discord channels"""
        # Create bot instance
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        
        bot = DiscordIntelligenceBot()
        
        try:
            # Login first (with timeout)
            self.stdout.write('Logging in to Discord...', ending='\n')
            self.stdout.flush()
            try:
                await asyncio.wait_for(bot.login(settings.DISCORD_BOT_TOKEN), timeout=15.0)
                self.stdout.write('✓ Login successful', ending='\n')
                self.stdout.flush()
            except asyncio.TimeoutError:
                self.stdout.write(self.style.ERROR('✗ Timeout during login (15s)'))
                raise
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'✗ Login error: {e}'))
                raise
            
            # Connect to gateway (this starts the websocket connection)
            self.stdout.write('Connecting to Discord gateway...', ending='\n')
            self.stdout.flush()
            # connect() is a coroutine that runs the websocket loop
            # We need to start it as a background task and wait for ready
            connect_task = asyncio.create_task(bot.connect(reconnect=False))
            
            # Wait for bot to be ready with timeout
            self.stdout.write('Waiting for bot to be ready...', ending='\n')
            self.stdout.flush()
            try:
                # Wait for ready event (this happens when connection is established)
                await asyncio.wait_for(bot.wait_until_ready(), timeout=60.0)
                self.stdout.write(self.style.SUCCESS('Connected to Discord and ready'))
            except asyncio.TimeoutError:
                # Cancel the connection task
                connect_task.cancel()
                try:
                    await connect_task
                except asyncio.CancelledError:
                    pass
                self.stdout.write(self.style.ERROR('Timeout waiting for bot to be ready (60s)'))
                raise
            except Exception as e:
                connect_task.cancel()
                self.stdout.write(self.style.ERROR(f'Connection error: {e}'))
                raise
            
            # Sync guild data to ensure channels are in database
            self.stdout.write('Syncing guild data...')
            await bot.sync_guild_data()
            self.stdout.write('Synced guild data')
            
            # Calculate cutoff time
            cutoff_time = datetime.now(timezone.utc) - timedelta(days=days)
            
            # Get channels to backfill
            channels_to_backfill = await self.get_channels_to_backfill(
                bot, channel_id, server_id, skip_existing, cutoff_time
            )
            
            if not channels_to_backfill:
                self.stdout.write(self.style.WARNING('No channels found to backfill'))
                return
            
            self.stdout.write(
                self.style.SUCCESS(f'Found {len(channels_to_backfill)} channel(s) to backfill')
            )
            
            total_messages = 0
            total_new = 0
            
            for channel_obj in channels_to_backfill:
                try:
                    new_count = await self.backfill_channel(
                        bot, channel_obj, cutoff_time, limit
                    )
                    total_new += new_count
                    total_messages += new_count
                    
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'✓ #{channel_obj.name}: {new_count} new messages'
                        )
                    )
                except Exception as e:
                    self.stdout.write(
                        self.style.ERROR(f'✗ Error backfilling #{channel_obj.name}: {e}')
                    )
                    logger.error(f"Error backfilling channel {channel_obj.channel_id}: {e}")
            
            self.stdout.write(
                self.style.SUCCESS(
                    f'\nBackfill complete! Total: {total_new} new messages across {len(channels_to_backfill)} channels'
                )
            )
            
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('\nBackfill interrupted by user'))
            raise
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error during backfill: {e}'))
            logger.error(f"Backfill error: {e}", exc_info=True)
        finally:
            try:
                if not bot.is_closed():
                    await bot.close()
                self.stdout.write('Disconnected from Discord')
            except Exception as e:
                logger.error(f"Error closing bot: {e}")

    async def get_channels_to_backfill(self, bot, channel_id, server_id, skip_existing, cutoff_time):
        """Get list of channels to backfill"""
        channels = []
        
        if channel_id:
            # Specific channel
            try:
                discord_channel = await sync_to_async(DiscordChannel.objects.get)(
                    channel_id=channel_id
                )
                channels.append(discord_channel)
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f'Channel {channel_id} not found in database: {e}')
                )
                return []
        else:
            # Get all text channels from database (including threads)
            # Filter for text channels and any channel type starting with 'thread_'
            if server_id:
                channels_query = DiscordChannel.objects.filter(
                    server__server_id=server_id
                ).filter(
                    Q(channel_type='text') | Q(channel_type__startswith='thread_')
                )
            else:
                channels_query = DiscordChannel.objects.filter(
                    Q(channel_type='text') | Q(channel_type__startswith='thread_')
                )
            
            channels = await sync_to_async(list)(channels_query)
        
        # Filter out channels that already have recent messages if skip_existing is True
        if skip_existing:
            filtered_channels = []
            for channel_obj in channels:
                has_recent = await sync_to_async(
                    DiscordMessage.objects.filter(
                        channel=channel_obj,
                        timestamp__gte=cutoff_time
                    ).exists
                )()
                
                if not has_recent:
                    filtered_channels.append(channel_obj)
                else:
                    self.stdout.write(
                        f'Skipping #{channel_obj.name} (already has recent messages)'
                    )
            
            channels = filtered_channels
        
        return channels

    async def backfill_channel(self, bot, channel_obj, cutoff_time, limit):
        """Backfill messages for a single channel"""
        try:
            # Get Discord channel object
            discord_channel = bot.get_channel(channel_obj.channel_id)
            
            if not discord_channel:
                # Try fetching if not in cache
                try:
                    discord_channel = await bot.fetch_channel(channel_obj.channel_id)
                except discord.NotFound:
                    self.stdout.write(
                        self.style.WARNING(
                            f'Channel {channel_obj.channel_id} not found on Discord'
                        )
                    )
                    return 0
                except discord.Forbidden:
                    self.stdout.write(
                        self.style.WARNING(
                            f'No permission to access channel {channel_obj.channel_id}'
                        )
                    )
                    return 0
            
            # Skip if not a text channel or thread
            if not isinstance(discord_channel, (discord.TextChannel, discord.Thread)):
                return 0
            
            # Get existing message IDs to avoid duplicates
            existing_ids = await sync_to_async(set)(
                DiscordMessage.objects.filter(
                    channel=channel_obj
                ).values_list('message_id', flat=True)
            )
            
            new_count = 0
            
            self.stdout.write(f'  Backfilling #{channel_obj.name}...', ending='')
            
            try:
                # Fetch messages in batches
                # Use after parameter to filter by date, and limit if specified
                async for message in discord_channel.history(
                    limit=limit,
                    after=cutoff_time,
                    oldest_first=True  # Process oldest first
                ):
                    # Skip if already exists
                    if message.id in existing_ids:
                        continue
                    
                    # Skip bot messages
                    if message.author.bot:
                        continue
                    
                    # Store the message
                    try:
                        await bot.store_message(message)
                        new_count += 1
                        
                        # Progress indicator
                        if new_count % 50 == 0:
                            self.stdout.write('.', ending='')
                            self.stdout.flush()
                    except Exception as e:
                        logger.error(f"Error storing message {message.id}: {e}")
                    
                    last_message_id = message.id
                    
                    # Rate limit handling - small delay between messages
                    await asyncio.sleep(0.1)
                
                self.stdout.write('')  # New line after progress dots
                
            except discord.Forbidden:
                self.stdout.write(
                    self.style.WARNING(f'  No permission to read #{channel_obj.name}')
                )
            except discord.HTTPException as e:
                self.stdout.write(
                    self.style.ERROR(f'  HTTP error for #{channel_obj.name}: {e}')
                )
            
            return new_count
            
        except Exception as e:
            logger.error(f"Error in backfill_channel for {channel_obj.channel_id}: {e}")
            raise
