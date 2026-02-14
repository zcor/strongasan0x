import discord
from discord.ext import commands
import asyncio
import logging
from django.conf import settings
from asgiref.sync import sync_to_async
from datetime import timezone
from rollcall.models import DiscordUserMapping
from rollcall.discord_bot.cogs.attestation import AttestationCog
from rollcall.discord_bot.cogs.ranking import RankingCog
from rollcall.discord_bot.cogs.admin import AdminCog

logger = logging.getLogger(__name__)


class GarminDiscordBot(commands.Bot):
    """Discord bot for Garmin project - manages rankings, attestations, and permissions"""
    
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        intents.reactions = True
        
        super().__init__(command_prefix='!', intents=intents)
    
    async def on_ready(self):
        """Called when the bot is ready"""
        logger.info(f'{self.user} has connected to Discord!')
        
        # Load cogs
        try:
            await self.add_cog(AttestationCog(self))
            logger.info('AttestationCog loaded successfully')
        except Exception as e:
            logger.error(f'Error loading AttestationCog: {e}')
        
        try:
            await self.add_cog(RankingCog(self))
            logger.info('RankingCog loaded successfully')
        except Exception as e:
            logger.error(f'Error loading RankingCog: {e}')
        
        try:
            await self.add_cog(AdminCog(self))
            logger.info('AdminCog loaded successfully')
        except Exception as e:
            logger.error(f'Error loading AdminCog: {e}')
        
        # Log registered commands
        logger.info(f'Registered commands: {[cmd.name for cmd in self.commands]}')
    
    async def on_message(self, message):
        """Handle incoming messages"""
        if message.author.bot:
            return
        
        # Log all messages for later review
        try:
            from rollcall.bot_commands.log_message import log_message
            user_mapping = await self.get_or_create_user_mapping(message.author)
            
            # Collect attachment information
            attachment_info = {}
            if message.attachments:
                attachment_info['attachments'] = []
                for att in message.attachments:
                    att_data = {
                        'id': att.id,
                        'filename': att.filename,
                        'size': att.size,
                        'url': att.url,
                        'proxy_url': att.proxy_url,
                        'content_type': att.content_type,
                    }
                    if hasattr(att, 'width') and att.width:
                        att_data['width'] = att.width
                    if hasattr(att, 'height') and att.height:
                        att_data['height'] = att.height
                    attachment_info['attachments'].append(att_data)
            
            await log_message(
                source='discord',
                user_mapping=user_mapping,
                message_id=message.id,
                chat_id=message.channel.id,
                content=message.content or '',
                posted_at=message.created_at.replace(tzinfo=timezone.utc),
                has_attachments=len(message.attachments) > 0,
                attachment_count=len(message.attachments),
                attachment_info=attachment_info
            )
        except Exception as e:
            logger.warning(f"Error logging Discord message: {e}")
        
        await self.process_commands(message)
    
    async def get_or_create_user_mapping(self, member):
        """Get or create DiscordUserMapping for a Discord member"""
        try:
            return await sync_to_async(DiscordUserMapping.objects.get)(
                discord_user_id=member.id,
                is_active=True
            )
        except DiscordUserMapping.DoesNotExist:
            # Create new mapping
            mapping = await sync_to_async(DiscordUserMapping.objects.create)(
                discord_user_id=member.id,
                discord_username=member.name,
                discord_display_name=member.display_name or member.name,
                is_active=True
            )
            logger.info(f"Created DiscordUserMapping for {member.name} (ID: {member.id})")
            return mapping
    
    async def update_user_mapping(self, member):
        """Update DiscordUserMapping with current Discord user info"""
        try:
            mapping = await sync_to_async(DiscordUserMapping.objects.get)(
                discord_user_id=member.id
            )
            mapping.discord_username = member.name
            mapping.discord_display_name = member.display_name or member.name
            await sync_to_async(mapping.save)()
        except DiscordUserMapping.DoesNotExist:
            # Create if doesn't exist
            await self.get_or_create_user_mapping(member)


# Global bot instance
bot = None


async def start_bot():
    """Start the Discord bot"""
    global bot
    
    if not settings.DISCORD_BOT_TOKEN:
        logger.error("DISCORD_BOT_TOKEN not set in environment variables")
        return
    
    bot = GarminDiscordBot()
    
    try:
        await bot.start(settings.DISCORD_BOT_TOKEN)
    except Exception as e:
        logger.error(f"Error starting bot: {e}")


def run_bot():
    """Run the bot in the event loop"""
    asyncio.run(start_bot())

