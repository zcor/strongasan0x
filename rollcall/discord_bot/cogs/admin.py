import discord
from discord.ext import commands
import logging
from asgiref.sync import sync_to_async
from rollcall.models import DiscordUserMapping, RollCallRanking

logger = logging.getLogger(__name__)


class AdminCog(commands.Cog):
    """Cog for admin commands to manage Discord user mappings"""
    
    def __init__(self, bot):
        self.bot = bot
    
    @commands.command(name='link')
    @commands.has_permissions(administrator=True)
    async def link_command(self, ctx, member: discord.Member, *, name_or_handle: str):
        """
        Link a Discord user to a contest participant by name or Twitter handle
        
        Usage:
            !link @username RektDiomedes
            !link @username @rektdiomedes
        """
        # Clean the name/handle
        name_or_handle = name_or_handle.strip().lstrip('@')
        
        # Try to find matching ranking by name or twitter handle
        def find_ranking():
            # Try by name first
            ranking = RollCallRanking.objects.filter(name__iexact=name_or_handle).first()
            if ranking:
                return ranking, 'name'
            
            # Try by twitter handle
            ranking = RollCallRanking.objects.filter(twitter_handle__iexact=name_or_handle).first()
            if ranking:
                return ranking, 'twitter_handle'
            
            return None, None
        
        ranking, match_type = await sync_to_async(find_ranking)()
        
        if not ranking:
            await ctx.send(
                f"❌ No contest participant found matching '{name_or_handle}'. "
                f"Please check the name or Twitter handle."
            )
            return
        
        # Get or create user mapping
        def get_or_create_mapping():
            mapping, created = DiscordUserMapping.objects.get_or_create(
                discord_user_id=member.id,
                defaults={
                    'discord_username': member.name,
                    'discord_display_name': member.display_name or member.name,
                    'is_active': True
                }
            )
            
            # Update mapping
            mapping.discord_username = member.name
            mapping.discord_display_name = member.display_name or member.name
            
            if match_type == 'name':
                mapping.linked_name = ranking.name
                if ranking.twitter_handle:
                    mapping.linked_twitter_handle = ranking.twitter_handle
            elif match_type == 'twitter_handle':
                mapping.linked_twitter_handle = ranking.twitter_handle
                mapping.linked_name = ranking.name
            
            mapping.is_active = True
            mapping.save()
            
            return mapping, created
        
        mapping, created = await sync_to_async(get_or_create_mapping)()
        
        action = "Created" if created else "Updated"
        await ctx.send(
            f"✅ {action} link: {member.display_name} → {ranking.name} "
            f"(Rank {ranking.rank} in week of {ranking.weekly_roll_call.week_start_date})"
        )
    
    @commands.command(name='unlink')
    @commands.has_permissions(administrator=True)
    async def unlink_command(self, ctx, member: discord.Member):
        """
        Unlink a Discord user from contest participant
        
        Usage:
            !unlink @username
        """
        def unlink_mapping():
            try:
                mapping = DiscordUserMapping.objects.get(discord_user_id=member.id)
                mapping.linked_name = ''
                mapping.linked_twitter_handle = ''
                mapping.is_active = False
                mapping.save()
                return True
            except DiscordUserMapping.DoesNotExist:
                return False
        
        success = await sync_to_async(unlink_mapping)()
        
        if success:
            await ctx.send(f"✅ Unlinked {member.display_name}")
        else:
            await ctx.send(f"❌ No mapping found for {member.display_name}")
    
    @commands.command(name='list_links')
    @commands.has_permissions(administrator=True)
    async def list_links_command(self, ctx):
        """
        List all active Discord user mappings
        
        Usage:
            !list_links
        """
        def get_mappings():
            return list(DiscordUserMapping.objects.filter(is_active=True).order_by('discord_username'))
        
        mappings = await sync_to_async(get_mappings)()
        
        if not mappings:
            await ctx.send("No active mappings found.")
            return
        
        # Discord embed limit is 2000 chars, so we'll paginate if needed
        lines = []
        for mapping in mappings:
            link_info = "unlinked"
            if mapping.linked_name:
                link_info = mapping.linked_name
                if mapping.linked_twitter_handle:
                    link_info += f" (@{mapping.linked_twitter_handle})"
            
            lines.append(f"• {mapping.discord_username} → {link_info}")
        
        message = "**Active Discord User Mappings:**\n" + "\n".join(lines)
        
        # Split into chunks if too long
        if len(message) > 2000:
            chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
            for chunk in chunks:
                await ctx.send(chunk)
        else:
            await ctx.send(message)
    
    @commands.command(name='sync_user')
    @commands.has_permissions(administrator=True)
    async def sync_user_command(self, ctx, member: discord.Member = None):
        """
        Sync Discord user info (update username/display name)
        
        Usage:
            !sync_user - Sync your own info
            !sync_user @username - Sync another user's info
        """
        if member is None:
            member = ctx.author
        
        await self.bot.update_user_mapping(member)
        await ctx.send(f"✅ Synced user info for {member.display_name}")


