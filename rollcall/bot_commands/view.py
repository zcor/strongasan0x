"""
View command - shows full attestation for a user
"""
from rollcall.models import Attestation, WeeklyRollCall, DiscordUserMapping, TelegramUserMapping
from asgiref.sync import sync_to_async
from django.db.models import Q
from rollcall.utils.rollcalls import get_active_roll_call


async def get_view_message(username, source='discord'):
    """
    Get full attestation view for a user
    
    Args:
        username: Username or linked name to search for
        source: 'discord' or 'telegram'
    
    Returns:
        str: Formatted message with full attestation
    """
    def get_attestation_data():
        roll_call = get_active_roll_call(allow_create=False)
        if not roll_call:
            roll_call = WeeklyRollCall.objects.order_by('-week_start_date').first()
        if not roll_call:
            return None, None
        
        # Find user mapping
        if source == 'discord':
            user_mapping = DiscordUserMapping.objects.filter(
                Q(discord_username__iexact=username) | 
                Q(linked_name__iexact=username) |
                Q(linked_twitter_handle__iexact=username)
            ).first()
        else:
            user_mapping = TelegramUserMapping.objects.filter(
                Q(telegram_username__iexact=username) |
                Q(telegram_first_name__iexact=username) |
                Q(linked_name__iexact=username) |
                Q(linked_twitter_handle__iexact=username)
            ).first()
        
        if not user_mapping:
            return None, None
        
        # Get attestations for current week
        if source == 'discord':
            attestations = Attestation.objects.filter(
                weekly_roll_call=roll_call,
                discord_user=user_mapping,
                parent_attestation__isnull=True
            ).order_by('-posted_at')
        else:
            attestations = Attestation.objects.filter(
                weekly_roll_call=roll_call,
                telegram_user=user_mapping,
                parent_attestation__isnull=True
            ).order_by('-posted_at')
        
        return roll_call, list(attestations)
    
    roll_call, attestations = await sync_to_async(get_attestation_data)()
    
    if not roll_call:
        return "No roll call data available."
    
    if not attestations:
        return f"No attestation found for {username} for week of {roll_call.week_start_date}."
    
    # Get the most recent attestation
    main_attestation = attestations[0]
    
    # Get all parts
    parts = Attestation.objects.filter(
        Q(id=main_attestation.id) | Q(parent_attestation=main_attestation)
    ).order_by('part_number')
    
    # Build message
    lines = []
    user_name = main_attestation.user_mapping.linked_name or (main_attestation.discord_user.discord_username if main_attestation.source == 'discord' else main_attestation.telegram_user.telegram_first_name)
    
    lines.append(f"**Attestation for {user_name}**")
    lines.append(f"Week of {roll_call.week_start_date}")
    lines.append(f"Posted: {main_attestation.posted_at.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    
    # Add all parts
    for part in parts:
        if part.part_number > 1:
            lines.append(f"\n--- Part {part.part_number} ---")
        
        lines.append(part.raw_text)
        
        if part.has_attachments:
            lines.append(f"\n📎 {part.attachment_count} attachment(s)")
    
    return "\n".join(lines)


