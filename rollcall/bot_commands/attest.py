"""
Attest command - explicitly mark a message as an attestation
"""
from rollcall.models import Attestation
from asgiref.sync import sync_to_async
from rollcall.utils.rollcalls import get_active_roll_call


async def store_attestation(
    source,
    user_mapping,
    message_id,
    chat_id,
    text,
    posted_at,
    has_attachments=False,
    attachment_count=0,
    parent_attestation=None,
    part_number=1,
    attachment_info=None,
    image_extraction_info=None
):
    """
    Store an attestation in the database

    Args:
        source: 'discord' or 'telegram'
        user_mapping: DiscordUserMapping or TelegramUserMapping instance
        message_id: Message ID from the platform
        chat_id: Channel/chat ID
        text: Message text
        posted_at: datetime when message was posted
        has_attachments: Whether message has attachments
        attachment_count: Number of attachments
        parent_attestation: Parent Attestation if this is a part
        part_number: Part number if this is a part
        attachment_info: Dict with attachment metadata (file_ids, etc.)
        image_extraction_info: Dict with extraction metadata if text was extracted from image

    Returns:
        tuple: (success: bool, message: str, attestation: Attestation or None)
    """
    def create_attestation():
        from django.db import close_old_connections
        close_old_connections()
        roll_call = get_active_roll_call(reference_dt=posted_at)
        if not roll_call:
            return None, "No roll call found for attestation"
        
        # Check if already exists.
        # Telegram message_id is unique only within a chat — must include chat_id.
        if source == 'discord':
            existing = Attestation.objects.filter(
                source='discord',
                discord_message_id=message_id
            ).first()
        else:
            existing = Attestation.objects.filter(
                source='telegram',
                telegram_chat_id=chat_id,
                telegram_message_id=message_id
            ).first()

        if existing:
            return existing, "Attestation already stored"
        
        # Build parsed_data with attachment and extraction info
        parsed_data = {}
        if attachment_info:
            parsed_data['attachment_info'] = attachment_info
        if image_extraction_info:
            parsed_data['image_extraction'] = image_extraction_info
            parsed_data['extracted_from_image'] = True

        # Create attestation
        attestation_data = {
            'weekly_roll_call': roll_call,
            'source': source,
            'raw_text': text,
            'posted_at': posted_at,
            'has_attachments': has_attachments,
            'attachment_count': attachment_count,
            'parent_attestation': parent_attestation,
            'part_number': part_number,
            'parsed_data': parsed_data if parsed_data else {}
        }
        
        if source == 'discord':
            attestation_data['discord_user'] = user_mapping
            attestation_data['discord_message_id'] = message_id
            attestation_data['discord_channel_id'] = chat_id
        else:
            attestation_data['telegram_user'] = user_mapping
            attestation_data['telegram_message_id'] = message_id
            attestation_data['telegram_chat_id'] = chat_id
        
        attestation = Attestation.objects.create(**attestation_data)
        
        # Link to message log if it exists (composite key for telegram)
        try:
            from rollcall.models import MessageLog
            if source == 'discord':
                message_log = MessageLog.objects.filter(
                    source='discord',
                    discord_message_id=message_id
                ).first()
            else:
                message_log = MessageLog.objects.filter(
                    source='telegram',
                    telegram_chat_id=chat_id,
                    telegram_message_id=message_id
                ).first()

            if message_log:
                message_log.is_attestation = True
                message_log.attestation = attestation
                message_log.save()
        except Exception:
            # Don't fail if message log doesn't exist
            pass
        
        return attestation, "Attestation stored successfully"
    
    attestation, message = await sync_to_async(create_attestation)()
    
    if attestation:
        return True, message, attestation
    else:
        return False, message, None
