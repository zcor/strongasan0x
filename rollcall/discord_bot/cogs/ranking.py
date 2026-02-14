import discord
from discord.ext import commands
import logging
from django.conf import settings
from asgiref.sync import sync_to_async
from rollcall.models import WeeklyRollCall, RollCallRanking, DiscordUserMapping

logger = logging.getLogger(__name__)


class RankingCog(commands.Cog):
    """Cog for managing permissioned groups based on weekly rankings"""
    
    def __init__(self, bot):
        self.bot = bot
    
    async def update_permissions(self):
        """Update Discord roles and channel permissions based on latest rankings"""
        guild_id = getattr(settings, 'DISCORD_GUILD_ID', None)
        if not guild_id:
            logger.error("DISCORD_GUILD_ID not set")
            return False, "DISCORD_GUILD_ID not set"
        
        try:
            guild_id = int(guild_id)
        except (ValueError, TypeError):
            logger.error(f"Invalid DISCORD_GUILD_ID: {guild_id}")
            return False, f"Invalid DISCORD_GUILD_ID: {guild_id}"
        
        guild = self.bot.get_guild(guild_id)
        if not guild:
            logger.error(f"Guild {guild_id} not found")
            return False, f"Guild {guild_id} not found"
        
        # Get latest roll call
        def get_latest_roll_call():
            return WeeklyRollCall.objects.order_by('-week_start_date').first()
        
        roll_call = await sync_to_async(get_latest_roll_call)()
        if not roll_call:
            return False, "No roll call found"
        
        # Get rankings
        def get_rankings():
            return list(RollCallRanking.objects.filter(
                weekly_roll_call=roll_call
            ).order_by('rank'))
        
        rankings = await sync_to_async(get_rankings)()
        if not rankings:
            return False, "No rankings found for latest roll call"
        
        # Get role names from settings
        top_10_role_name = getattr(settings, 'DISCORD_TOP_10_ROLE_NAME', 'Top Ten')
        top_5_role_name = getattr(settings, 'DISCORD_TOP_5_ROLE_NAME', 'Top 5')
        
        # Find or create roles - try both "Top Ten" and "Top 10" for compatibility
        top_10_role = discord.utils.get(guild.roles, name=top_10_role_name)
        if not top_10_role and top_10_role_name == 'Top Ten':
            top_10_role = discord.utils.get(guild.roles, name='Top 10')
            if top_10_role:
                top_10_role_name = 'Top 10'  # Use existing role name
        top_5_role = discord.utils.get(guild.roles, name=top_5_role_name)
        
        if not top_10_role:
            try:
                top_10_role = await guild.create_role(
                    name=top_10_role_name,
                    reason="Auto-created for weekly rankings"
                )
                logger.info(f"Created role: {top_10_role_name}")
            except discord.Forbidden:
                return False, f"Bot does not have permission to create role: {top_10_role_name}"
            except Exception as e:
                return False, f"Error creating role {top_10_role_name}: {e}"
        
        if not top_5_role:
            try:
                top_5_role = await guild.create_role(
                    name=top_5_role_name,
                    reason="Auto-created for weekly rankings"
                )
                logger.info(f"Created role: {top_5_role_name}")
            except discord.Forbidden:
                return False, f"Bot does not have permission to create role: {top_5_role_name}"
            except Exception as e:
                return False, f"Error creating role {top_5_role_name}: {e}"
        
        # Get all user mappings
        def get_user_mappings():
            return list(DiscordUserMapping.objects.filter(is_active=True))
        
        user_mappings = await sync_to_async(get_user_mappings)()
        
        # Build sets of users who should have each role
        top_10_user_ids = set()
        top_5_user_ids = set()
        
        for ranking in rankings:
            if ranking.rank <= 10:
                # Find user mapping by name or twitter handle
                # Try exact match first, then case-insensitive, then partial match
                mapping_found = False
                for mapping in user_mappings:
                    # Exact match on linked_name
                    if mapping.linked_name and mapping.linked_name == ranking.name:
                        top_10_user_ids.add(mapping.discord_user_id)
                        if ranking.rank <= 5:
                            top_5_user_ids.add(mapping.discord_user_id)
                        mapping_found = True
                        break
                    
                    # Case-insensitive match on linked_name
                    if mapping.linked_name and mapping.linked_name.lower() == ranking.name.lower():
                        top_10_user_ids.add(mapping.discord_user_id)
                        if ranking.rank <= 5:
                            top_5_user_ids.add(mapping.discord_user_id)
                        mapping_found = True
                        break
                    
                    # Partial match - check if ranking name contains linked_name or vice versa
                    if mapping.linked_name:
                        linked_lower = mapping.linked_name.lower()
                        ranking_lower = ranking.name.lower()
                        # Remove common prefixes/suffixes for matching
                        linked_clean = linked_lower.replace('@', '').replace('_', '').replace(' ', '')
                        ranking_clean = ranking_lower.replace('@', '').replace('_', '').replace(' ', '')
                        if linked_clean == ranking_clean or linked_clean in ranking_clean or ranking_clean in linked_clean:
                            top_10_user_ids.add(mapping.discord_user_id)
                            if ranking.rank <= 5:
                                top_5_user_ids.add(mapping.discord_user_id)
                            mapping_found = True
                            break
                    
                    # Twitter handle match
                    if ranking.twitter_handle and mapping.linked_twitter_handle:
                        twitter_match = mapping.linked_twitter_handle.lower().lstrip('@') == ranking.twitter_handle.lower().lstrip('@')
                        if twitter_match:
                            top_10_user_ids.add(mapping.discord_user_id)
                            if ranking.rank <= 5:
                                top_5_user_ids.add(mapping.discord_user_id)
                            mapping_found = True
                            break
                
                if not mapping_found:
                    logger.warning(f"No Discord mapping found for ranking: {ranking.name} (rank {ranking.rank})")
        
        # Update roles for all members
        updated_count = 0
        errors = []
        
        for member in guild.members:
            if member.bot:
                continue
            
            member_id = member.id
            should_have_top_10 = member_id in top_10_user_ids
            should_have_top_5 = member_id in top_5_user_ids
            
            has_top_10 = top_10_role in member.roles
            has_top_5 = top_5_role in member.roles
            
            try:
                # Add/remove top 10 role
                if should_have_top_10 and not has_top_10:
                    await member.add_roles(top_10_role, reason="Weekly ranking update")
                    updated_count += 1
                    logger.info(f"Added {top_10_role_name} to {member.display_name}")
                elif not should_have_top_10 and has_top_10:
                    await member.remove_roles(top_10_role, reason="Weekly ranking update")
                    updated_count += 1
                    logger.info(f"Removed {top_10_role_name} from {member.display_name}")
                
                # Add/remove top 5 role
                if should_have_top_5 and not has_top_5:
                    await member.add_roles(top_5_role, reason="Weekly ranking update")
                    updated_count += 1
                    logger.info(f"Added {top_5_role_name} to {member.display_name}")
                elif not should_have_top_5 and has_top_5:
                    await member.remove_roles(top_5_role, reason="Weekly ranking update")
                    updated_count += 1
                    logger.info(f"Removed {top_5_role_name} from {member.display_name}")
            except discord.Forbidden:
                errors.append(f"No permission to update roles for {member.display_name}")
            except Exception as e:
                errors.append(f"Error updating {member.display_name}: {e}")
        
        # Update channel permissions if configured
        top_10_channel_id = getattr(settings, 'DISCORD_TOP_10_CHANNEL_ID', None)
        top_5_channel_id = getattr(settings, 'DISCORD_TOP_5_CHANNEL_ID', None)
        
        if top_10_channel_id:
            try:
                channel = guild.get_channel(int(top_10_channel_id))
                if channel:
                    # Set channel to only be visible to top 10 role
                    overwrite = channel.overwrites_for(top_10_role)
                    overwrite.view_channel = True
                    overwrite.send_messages = True
                    overwrite.read_messages = True
                    await channel.set_permissions(top_10_role, overwrite=overwrite)
                    
                    # Hide from @everyone
                    overwrite_everyone = channel.overwrites_for(guild.default_role)
                    overwrite_everyone.view_channel = False
                    await channel.set_permissions(guild.default_role, overwrite=overwrite_everyone)
                    logger.info(f"Updated permissions for top 10 channel: {channel.name}")
            except Exception as e:
                errors.append(f"Error updating top 10 channel permissions: {e}")
        
        if top_5_channel_id:
            try:
                channel = guild.get_channel(int(top_5_channel_id))
                if channel:
                    # Set channel to only be visible to top 5 role
                    overwrite = channel.overwrites_for(top_5_role)
                    overwrite.view_channel = True
                    overwrite.send_messages = True
                    overwrite.read_messages = True
                    await channel.set_permissions(top_5_role, overwrite=overwrite)
                    
                    # Hide from @everyone
                    overwrite_everyone = channel.overwrites_for(guild.default_role)
                    overwrite_everyone.view_channel = False
                    await channel.set_permissions(guild.default_role, overwrite=overwrite_everyone)
                    logger.info(f"Updated permissions for top 5 channel: {channel.name}")
            except Exception as e:
                errors.append(f"Error updating top 5 channel permissions: {e}")
        
        message = f"Updated permissions: {updated_count} role changes"
        if errors:
            message += f"\nErrors: {len(errors)}"
            logger.warning(f"Permission update errors: {errors}")
        
        return True, message
    
    @commands.command(name='update_ranks')
    @commands.has_permissions(administrator=True)
    async def update_ranks_command(self, ctx):
        """
        Manually update Discord permissions based on latest rankings
        
        Usage:
            !update_ranks - Update roles and channel permissions
        """
        async with ctx.typing():
            success, message = await self.update_permissions()
            if success:
                await ctx.send(f"✅ {message}")
            else:
                await ctx.send(f"❌ {message}")


