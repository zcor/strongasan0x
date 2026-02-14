"""
Utility function to log all messages from Discord and Telegram
"""
import logging
from asgiref.sync import sync_to_async
from datetime import timezone
from rollcall.models import MessageLog

logger = logging.getLogger(__name__)


async def log_message(
    source,
    user_mapping,
    message_id,
    chat_id,
    content,
    posted_at,
    has_attachments=False,
    attachment_count=0,
    attachment_info=None
):
    """
    Log a message to the message log for later review
    
    Args:
        source: 'discord' or 'telegram'
        user_mapping: DiscordUserMapping or TelegramUserMapping instance
        message_id: Message ID from the platform
        chat_id: Channel/chat ID
        content: Message text
        posted_at: datetime when message was posted
        has_attachments: Whether message has attachments
        attachment_count: Number of attachments
    
    Returns:
        tuple: (success: bool, message: str, message_log: MessageLog or None)
    """
    def create_message_log():
        from django.db import close_old_connections
        close_old_connections()
        # Ensure posted_at is timezone-aware
        posted_at_tz = posted_at
        if posted_at_tz.tzinfo is None:
            posted_at_tz = posted_at_tz.replace(tzinfo=timezone.utc)
        elif posted_at_tz.tzinfo != timezone.utc:
            posted_at_tz = posted_at_tz.astimezone(timezone.utc)
        
        # Prepare data based on source
        log_data = {
            'source': source,
            'content': content,
            'posted_at': posted_at_tz,
            'has_attachments': has_attachments,
            'attachment_count': attachment_count,
            'attachment_info': attachment_info or {},
        }
        
        if source == 'discord':
            log_data['discord_user'] = user_mapping
            log_data['discord_message_id'] = message_id
            log_data['discord_channel_id'] = chat_id
        else:
            log_data['telegram_user'] = user_mapping
            log_data['telegram_message_id'] = message_id
            log_data['telegram_chat_id'] = chat_id
        
        # Use get_or_create to avoid duplicates
        message_log, created = MessageLog.objects.get_or_create(
            **{f'{source}_message_id': message_id},
            defaults=log_data
        )
        
        if not created:
            # Update existing log
            for key, value in log_data.items():
                setattr(message_log, key, value)
            message_log.save()
        
        return message_log, created
    
    try:
        message_log, created = await sync_to_async(create_message_log)()
        action = "Created" if created else "Updated"
        return True, f"{action} message log", message_log
    except Exception as e:
        logger.error(f"Error logging message: {e}")
        return False, f"Error logging message: {str(e)}", None

