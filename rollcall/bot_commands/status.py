"""
Status command - shows attestation status for all users
"""
from datetime import timedelta
from rollcall.models import WeeklyRollCall, RollCallRanking, Attestation
from asgiref.sync import sync_to_async
from rollcall.utils.rollcalls import get_active_roll_call


def escape_markdown(text):
    """Escape Telegram Markdown special characters"""
    if not text:
        return text
    # Escape underscores, asterisks, brackets, backticks
    for char in ['_', '*', '[', ']', '`']:
        text = text.replace(char, '\\' + char)
    return text


async def get_status_message():
    """
    Generate status message showing:
    - Prior week's ranked users
    - New users who submitted attestations
    - Current week submission status (✅ or ❌)
    - Preview of attestations with attachment indicators
    """
    def get_data():
        current_roll_call = get_active_roll_call()

        # Get the most recently PUBLISHED roll call *before* the current week
        # This is the week whose rankings we use for comparison ("Prior Week's Rankings")
        prior_published_roll_call = WeeklyRollCall.objects.filter(
            is_published=True,
            week_start_date__lt=current_roll_call.week_start_date # Weeks ending before current week's monday
        ).order_by('-week_start_date').first()
        
        # In this case, prior_rankings will be an empty list if no prior_published_roll_call is found.
        prior_rankings = []
        if prior_published_roll_call:
            prior_rankings = list(RollCallRanking.objects.filter(
                weekly_roll_call=prior_published_roll_call
            ).order_by('rank'))
        
        # Get all users who submitted attestations for current week
        current_attestations = Attestation.objects.filter(
            weekly_roll_call=current_roll_call,
            parent_attestation__isnull=True  # Only main attestations, not parts
        ).select_related('discord_user', 'telegram_user')
        
        # Build user status map
        user_status = {}
        
        # Add ranked users from prior week's published rankings
        if prior_published_roll_call and prior_rankings: # Only add if there was a prior published week with rankings
            for ranking in prior_rankings:
                user_status[ranking.name] = {
                    'rank': ranking.rank,
                    'has_attestation': False, # Assume false until proven true for current week
                    'attestation_preview': None,
                    'has_attachments': False,
                    'is_ranked': True # Was ranked in prior week
                }
        
        # Check attestations against the current week's roll call
        for attestation in current_attestations:
            user_mapping = attestation.user_mapping
            if not user_mapping:
                continue
            
            # Use linked_name if available, otherwise try to match by other means
            name = user_mapping.linked_name
            if not name:
                # If no linked_name, try discord_username or telegram_first_name
                if attestation.source == 'discord' and attestation.discord_user:
                    name = attestation.discord_user.discord_username
                elif attestation.source == 'telegram' and attestation.telegram_user:
                    name = attestation.telegram_user.telegram_first_name
                if not name:
                    continue # Skip if no identifiable name
            
            # Normalize name for matching (case-insensitive)
            name_lower = name.lower().strip()
            
            # Try to find matching entry in user_status (case-insensitive)
            matched_name = None
            for status_name in user_status.keys():
                if status_name.lower().strip() == name_lower:
                    matched_name = status_name
                    break
            
            if matched_name:
                # Update existing entry
                user_status[matched_name]['has_attestation'] = True
                name = matched_name  # Use the matched name for consistency
            else:
                # New user not in prior rankings, but submitted this week
                if name not in user_status:
                    user_status[name] = {
                        'rank': None, # Not ranked in prior week
                        'has_attestation': False, # Will be set to true below
                        'attestation_preview': None,
                        'has_attachments': False,
                        'is_ranked': False # Not ranked in prior week
                    }
                user_status[name]['has_attestation'] = True
            
            # Get full text from all parts
            from django.db.models import Q
            parts = list(Attestation.objects.filter(
                Q(id=attestation.id) | Q(parent_attestation=attestation)
            ).order_by('part_number'))
            
            full_text = ' '.join([part.raw_text for part in parts])
            has_attachments = any(part.has_attachments for part in parts)
            
            # Preview first 50-60 characters
            preview = full_text[:60].strip()
            if len(full_text) > 60:
                preview += '...'
            
            user_status[name]['attestation_preview'] = preview
            user_status[name]['has_attachments'] = has_attachments
        
        return prior_published_roll_call, current_roll_call, prior_rankings, user_status
    
    # Unpack the tuple correctly
    prior_published_roll_call, current_roll_call, prior_rankings, user_status = await sync_to_async(get_data)()
    
    if not current_roll_call: # Fallback if no current roll call could be found/created
        return "No current roll call data available for processing attestations."
    
    # Calculate publication date for current_roll_call
    publication_date = current_roll_call.week_end_date + timedelta(days=1)

    # Build status message
    lines = []
    lines.append("**Attestation Status**")
    lines.append(f"**Week of {current_roll_call.week_start_date.strftime('%b %d, %Y')} - {current_roll_call.week_end_date.strftime('%b %d, %Y')}**")
    lines.append(f"*(Published: {publication_date.strftime('%b %d, %Y')})*\n")
    
    # Ranked users section
    if prior_published_roll_call and prior_rankings:
        lines.append(f"**Prior Week's Rankings (Week of {prior_published_roll_call.week_start_date.strftime('%b %d, %Y')}):**")
        for ranking in prior_rankings[:10]:  # Top 10
            name = ranking.name
            status = user_status.get(name, {})
            check = "✅" if status.get('has_attestation') else "❌"
            preview = status.get('attestation_preview', '')
            if preview:
                preview = ' '.join(preview.split())  # Replace all whitespace/newlines with single spaces
            attachment = "📎" if status.get('has_attachments') else ""

            # Escape markdown special chars in name and preview
            escaped_name = escape_markdown(name)
            escaped_preview = escape_markdown(preview)

            line = f"{check} #{ranking.rank} {escaped_name}"
            if escaped_preview:
                line += f" - {escaped_preview}"
            if attachment:
                line += f" {attachment}"
            lines.append(line)
    elif prior_published_roll_call: # If there was a prior published roll call, but no rankings in it
        lines.append(f"**Prior Week's Rankings (Week of {prior_published_roll_call.week_start_date.strftime('%b %d, %Y')}):**")
        lines.append("No rankings were available for the prior published week.")
    else: # No prior published roll call at all
        lines.append("**Prior Week's Rankings:**")
        lines.append("No prior published week found.")
    
    # New users section
    new_users = [name for name, data in user_status.items() if not data.get('is_ranked') and data.get('has_attestation')]
    if new_users:
        lines.append("\n**New Submissions (Not Ranked Last Week):**")
        for name in new_users:
            status = user_status[name]
            preview = status.get('attestation_preview', '')
            if preview:
                preview = ' '.join(preview.split())  # Replace all whitespace/newlines with single spaces
            attachment = "📎" if status.get('has_attachments') else ""

            # Escape markdown special chars in name and preview
            escaped_name = escape_markdown(name)
            escaped_preview = escape_markdown(preview)

            line = f"✅ {escaped_name}"
            if escaped_preview:
                line += f" - {escaped_preview}"
            if attachment:
                line += f" {attachment}"
            lines.append(line)

    # Login link
    lines.append("\n---")
    lines.append("View/edit your attestations: https://strongasan0x.com/warrior/")

    return "\n".join(lines)
