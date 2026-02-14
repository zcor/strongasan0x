from django.core.management.base import BaseCommand
from django.conf import settings
import asyncio
import logging
import discord
from rollcall.models import WeeklyRollCall, RollCallRanking, DiscordUserMapping
from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Assign Top Ten role to all Top 10 users from recent roll calls'

    def add_arguments(self, parser):
        parser.add_argument(
            '--weeks',
            type=int,
            default=2,
            help='Number of recent weeks to process (default: 2)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without making changes',
        )

    def handle(self, *args, **options):
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

        weeks = options.get('weeks', 2)
        dry_run = options.get('dry_run', False)

        async def assign_roles():
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
                        await guild.chunk()
                    except discord.HTTPException as e:
                        self.stdout.write(self.style.ERROR(f"Could not fetch guild {guild_id}: {e}"))
                        return
                
                # Get Top Ten role - try both "Top Ten" and "Top 10"
                top_10_role_name = getattr(settings, 'DISCORD_TOP_10_ROLE_NAME', 'Top Ten')
                top_10_role = discord.utils.get(guild.roles, name=top_10_role_name)
                
                # Fallback to "Top 10" if "Top Ten" not found
                if not top_10_role and top_10_role_name == 'Top Ten':
                    top_10_role = discord.utils.get(guild.roles, name='Top 10')
                    if top_10_role:
                        self.stdout.write(
                            self.style.WARNING(
                                f"Found role 'Top 10' (will use this instead of 'Top Ten')"
                            )
                        )
                        top_10_role_name = 'Top 10'
                
                if not top_10_role:
                    self.stdout.write(
                        self.style.ERROR(
                            f"Role '{top_10_role_name}' or 'Top 10' not found. Please create it first."
                        )
                    )
                    return
                
                # Get recent roll calls
                def get_recent_roll_calls():
                    return list(WeeklyRollCall.objects.order_by('-week_start_date')[:weeks])
                
                roll_calls = await sync_to_async(get_recent_roll_calls)()
                
                if not roll_calls:
                    self.stdout.write(self.style.ERROR("No roll calls found"))
                    return
                
                self.stdout.write(f"\n📊 Processing {len(roll_calls)} recent roll call(s)...\n")
                
                # Get all user mappings
                def get_user_mappings():
                    return list(DiscordUserMapping.objects.filter(is_active=True))
                
                user_mappings = await sync_to_async(get_user_mappings)()
                
                # Collect all Top 10 user IDs from recent weeks
                top_10_user_ids = set()
                processed_rankings = []
                
                def find_mapping_for_ranking(ranking_name, ranking_twitter_handle=None):
                    """Find DiscordUserMapping for a ranking name with improved matching"""
                    ranking_name_lower = ranking_name.lower().strip()
                    ranking_clean = ranking_name_lower.replace('@', '').replace('_', '').replace(' ', '').replace('|', '').replace('-', '')
                    
                    for mapping in user_mappings:
                        # Skip fake IDs
                        if mapping.discord_user_id >= 900000000000000000:
                            continue
                        
                        # Exact match on linked_name
                        if mapping.linked_name:
                            if mapping.linked_name == ranking_name:
                                return mapping
                            if mapping.linked_name.lower() == ranking_name_lower:
                                return mapping
                            
                            # Clean comparison
                            linked_clean = mapping.linked_name.lower().replace('@', '').replace('_', '').replace(' ', '').replace('|', '').replace('-', '')
                            if linked_clean == ranking_clean:
                                return mapping
                            
                            # Partial match - check if one contains the other
                            if ranking_clean and linked_clean:
                                if ranking_clean in linked_clean or linked_clean in ranking_clean:
                                    # Make sure it's a substantial match (at least 3 chars)
                                    if len(ranking_clean) >= 3 and len(linked_clean) >= 3:
                                        return mapping
                        
                        # Twitter handle match
                        if ranking_twitter_handle and mapping.linked_twitter_handle:
                            twitter_clean = ranking_twitter_handle.lower().lstrip('@').strip()
                            linked_twitter_clean = mapping.linked_twitter_handle.lower().lstrip('@').strip()
                            if twitter_clean == linked_twitter_clean:
                                return mapping
                    
                    return None
                
                for roll_call in roll_calls:
                    def get_rankings():
                        return list(RollCallRanking.objects.filter(
                            weekly_roll_call=roll_call,
                            rank__lte=10
                        ).order_by('rank'))
                    
                    rankings = await sync_to_async(get_rankings)()
                    
                    self.stdout.write(f"\n📅 Week of {roll_call.week_start_date}:")
                    
                    for ranking in rankings:
                        mapping = find_mapping_for_ranking(ranking.name, ranking.twitter_handle)
                        
                        if mapping:
                            top_10_user_ids.add(mapping.discord_user_id)
                            processed_rankings.append({
                                'rank': ranking.rank,
                                'name': ranking.name,
                                'discord_id': mapping.discord_user_id,
                                'week': roll_call.week_start_date
                            })
                            self.stdout.write(
                                f"  Rank {ranking.rank}: {ranking.name} → Discord ID {mapping.discord_user_id} ✅"
                            )
                        else:
                            self.stdout.write(
                                self.style.WARNING(f"  Rank {ranking.rank}: {ranking.name} → No Discord mapping found ⚠️")
                            )
                
                self.stdout.write(f"\n\n👥 Found {len(top_10_user_ids)} unique Top 10 users across {len(roll_calls)} week(s)\n")
                
                if dry_run:
                    self.stdout.write(self.style.WARNING("🔍 DRY RUN MODE - No changes will be made\n"))
                
                # Assign role to all Top 10 users
                assigned_count = 0
                already_has_role = 0
                errors = []
                
                for user_id in top_10_user_ids:
                    member = guild.get_member(user_id)
                    if not member:
                        try:
                            member = await guild.fetch_member(user_id)
                        except:
                            errors.append(f"Could not find member with ID {user_id}")
                            continue
                    
                    if member.bot:
                        continue
                    
                    if top_10_role in member.roles:
                        already_has_role += 1
                        if not dry_run:
                            self.stdout.write(
                                f"  {member.display_name} (@{member.name}) - Already has {top_10_role_name} ✅"
                            )
                    else:
                        if dry_run:
                            self.stdout.write(
                                f"  {member.display_name} (@{member.name}) - Would assign {top_10_role_name} 🔄"
                            )
                        else:
                            try:
                                await member.add_roles(top_10_role, reason="Top 10 user from recent rankings")
                                assigned_count += 1
                                self.stdout.write(
                                    self.style.SUCCESS(
                                        f"  ✅ Assigned {top_10_role_name} to {member.display_name} (@{member.name})"
                                    )
                                )
                            except discord.Forbidden:
                                errors.append(f"No permission to assign role to {member.display_name}")
                            except Exception as e:
                                errors.append(f"Error assigning role to {member.display_name}: {e}")
                
                # Summary
                self.stdout.write("\n" + "=" * 60)
                if dry_run:
                    self.stdout.write(self.style.WARNING("DRY RUN SUMMARY:"))
                    self.stdout.write(f"  Would assign role to: {len(top_10_user_ids) - already_has_role} user(s)")
                    self.stdout.write(f"  Already have role: {already_has_role} user(s)")
                else:
                    self.stdout.write(self.style.SUCCESS("SUMMARY:"))
                    self.stdout.write(f"  ✅ Assigned role to: {assigned_count} user(s)")
                    self.stdout.write(f"  Already had role: {already_has_role} user(s)")
                
                if errors:
                    self.stdout.write(self.style.ERROR(f"\n  ⚠️ Errors: {len(errors)}"))
                    for error in errors:
                        self.stdout.write(self.style.ERROR(f"    - {error}"))
                
                # Check channel permissions
                top_10_channel_id = getattr(settings, 'DISCORD_TOP_10_CHANNEL_ID', None)
                if top_10_channel_id:
                    try:
                        channel = guild.get_channel(int(top_10_channel_id))
                        if channel:
                            overwrite = channel.overwrites_for(top_10_role)
                            if overwrite.view_channel is not True:
                                if dry_run:
                                    self.stdout.write(
                                        self.style.WARNING(
                                            f"\n  🔍 Would update channel permissions for #{channel.name}"
                                        )
                                    )
                                else:
                                    overwrite.view_channel = True
                                    overwrite.send_messages = True
                                    overwrite.read_message_history = True
                                    await channel.set_permissions(top_10_role, overwrite=overwrite)
                                    
                                    # Hide from @everyone if not already
                                    overwrite_everyone = channel.overwrites_for(guild.default_role)
                                    if overwrite_everyone.view_channel is not False:
                                        overwrite_everyone.view_channel = False
                                        await channel.set_permissions(guild.default_role, overwrite=overwrite_everyone)
                                    
                                    self.stdout.write(
                                        self.style.SUCCESS(
                                            f"\n  ✅ Updated channel permissions for #{channel.name}"
                                        )
                                    )
                            else:
                                self.stdout.write(
                                    f"\n  ✅ Channel #{channel.name} already has correct permissions"
                                )
                    except Exception as e:
                        self.stdout.write(
                            self.style.ERROR(f"\n  ⚠️ Error updating channel permissions: {e}")
                        )
                else:
                    self.stdout.write(
                        self.style.WARNING(
                            "\n  ⚠️ DISCORD_TOP_10_CHANNEL_ID not set - channel permissions not updated"
                        )
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
            asyncio.run(assign_roles())
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error: {e}'))
            logger.error(f'Error assigning Top Ten role: {e}', exc_info=True)
