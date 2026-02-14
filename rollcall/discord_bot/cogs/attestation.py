from discord.ext import commands
import logging
from django.conf import settings
from asgiref.sync import sync_to_async
from datetime import timezone, timedelta
from rollcall.models import Attestation
from rollcall.bot_commands import attest, status, view, delete_part
from django.db.models import Q
from rollcall.utils.rollcalls import get_active_roll_call

logger = logging.getLogger(__name__)


class AttestationCog(commands.Cog):
    """Cog for handling attestation collection from Discord"""
    
    def __init__(self, bot):
        self.bot = bot
    
    @commands.Cog.listener()
    async def on_message(self, message):
        """Listen for messages in the attestation channel"""
        if message.author.bot:
            return
        
        # Check if message is in the attestation channel
        attestation_channel_id = getattr(settings, 'DISCORD_ATTESTATION_CHANNEL_ID', None)
        if not attestation_channel_id:
            return
        
        try:
            attestation_channel_id = int(attestation_channel_id)
        except (ValueError, TypeError):
            logger.warning(f"Invalid DISCORD_ATTESTATION_CHANNEL_ID: {attestation_channel_id}")
            return
        
        if message.channel.id != attestation_channel_id:
            return
        
        # Get or create user mapping
        user_mapping = await self.bot.get_or_create_user_mapping(message.author)
        
        # Check for existing attestation in time window (multi-part detection)
        window_minutes = getattr(settings, 'ATTESTATION_MULTI_PART_WINDOW_MINUTES', 15)
        message_date = message.created_at.replace(tzinfo=timezone.utc)
        time_window_start = message_date - timedelta(minutes=window_minutes)
        
        def find_recent_attestation():
            roll_call = get_active_roll_call(reference_dt=message_date)
            if not roll_call:
                return None, 1
            
            recent = Attestation.objects.filter(
                weekly_roll_call=roll_call,
                discord_user=user_mapping,
                source='discord',
                posted_at__gte=time_window_start,
                posted_at__lte=message_date
            ).order_by('-posted_at').first()
            
            if recent:
                parent = recent.parent_attestation or recent
                parts = Attestation.objects.filter(
                    Q(id=parent.id) | Q(parent_attestation=parent)
                )
                max_part = max([p.part_number for p in parts]) if parts.exists() else 1
                return parent, max_part + 1
            
            return None, 1
        
        parent_attestation, part_number = await sync_to_async(find_recent_attestation)()
        
        # Store attestation using shared command
        success, message_text, attestation_obj = await attest.store_attestation(
            source='discord',
            user_mapping=user_mapping,
            message_id=message.id,
            chat_id=message.channel.id,
            text=message.content,
            posted_at=message_date,
            has_attachments=len(message.attachments) > 0,
            attachment_count=len(message.attachments),
            parent_attestation=parent_attestation,
            part_number=part_number
        )
        
        if success:
            logger.info(f"Stored attestation from {message.author.name} (ID: {message.id}, Part: {part_number})")
            # React to confirm
            try:
                await message.add_reaction('✅')
            except Exception as e:
                logger.warning(f"Could not add reaction to attestation: {e}")
        else:
            logger.warning(f"Failed to store attestation: {message_text}")
    
    @commands.command(name='attest')
    async def attest_command(self, ctx):
        """
        Explicitly mark your message as an attestation
        
        Usage:
            !attest - Mark your message as an attestation
        """
        # Get user mapping
        user_mapping = await self.bot.get_or_create_user_mapping(ctx.author)
        
        # Get message text
        text = ctx.message.content.replace('!attest', '', 1).strip()
        
        # If no text and message is a reply, use replied message
        if not text and ctx.message.reference:
            try:
                replied = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                text = replied.content
            except:
                pass
        
        if not text:
            await ctx.send(
                "Please provide your attestation text, or reply to a message with !attest"
            )
            return
        
        # Store attestation
        success, message_text, attestation_obj = await attest.store_attestation(
            source='discord',
            user_mapping=user_mapping,
            message_id=ctx.message.id,
            chat_id=ctx.channel.id,
            text=text,
            posted_at=ctx.message.created_at.replace(tzinfo=timezone.utc),
            has_attachments=len(ctx.message.attachments) > 0,
            attachment_count=len(ctx.message.attachments)
        )
        
        if success:
            # Get status to show after storing
            status_message = await status.get_status_message()
            response = f"✅ {message_text}\n\n{status_message}"
            await ctx.send(response)
        else:
            await ctx.send(f"❌ {message_text}")
    
    @commands.command(name='status')
    async def status_command(self, ctx):
        """
        View attestation status for all users
        
        Usage:
            !status - Show status of all users' attestations
        """
        try:
            status_message = await status.get_status_message()
            await ctx.send(status_message)
        except Exception as e:
            logger.error(f"Error in status command: {e}")
            await ctx.send("Error retrieving status. Please try again later.")
    
    @commands.command(name='view')
    async def view_command(self, ctx, *, username: str = None):
        """
        View full attestation for a user
        
        Usage:
            !view username - View full attestation for a user
        """
        if not username:
            await ctx.send("Usage: !view <username>")
            return
        
        try:
            view_message = await view.get_view_message(username, source='discord')
            await ctx.send(view_message)
        except Exception as e:
            logger.error(f"Error in view command: {e}")
            await ctx.send("Error retrieving attestation. Please try again later.")
    
    @commands.command(name='delete_part')
    async def delete_part_command(self, ctx, part_number: int = None):
        """
        Delete a part from your attestation
        
        Usage:
            !delete_part <part_number> - Delete a specific part
        """
        if part_number is None:
            await ctx.send("Usage: !delete_part <part_number>")
            return
        
        # Get user mapping
        user_mapping = await self.bot.get_or_create_user_mapping(ctx.author)
        
        # Find user's attestation for current week
        def get_user_attestation():
            roll_call = get_active_roll_call()
            
            if not roll_call:
                return None
            
            return Attestation.objects.filter(
                weekly_roll_call=roll_call,
                discord_user=user_mapping,
                source='discord',
                parent_attestation__isnull=True
            ).order_by('-posted_at').first()
        
        attestation = await sync_to_async(get_user_attestation)()
        
        if not attestation:
            await ctx.send("No attestation found for current week")
            return
        
        # Check if user is admin
        is_admin = ctx.author.guild_permissions.administrator
        
        success, message = await delete_part.delete_attestation_part(
            attestation.id,
            part_number,
            user_mapping=user_mapping,
            is_admin=is_admin
        )
        
        if success:
            await ctx.send(f"✅ {message}")
        else:
            await ctx.send(f"❌ {message}")
