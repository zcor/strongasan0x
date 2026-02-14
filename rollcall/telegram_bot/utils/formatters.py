"""
Formatting utilities for bot messages
"""


def format_attestation_preview(text, max_length=60):
    """
    Format attestation text for preview
    
    Args:
        text: Full attestation text
        max_length: Maximum length for preview
    
    Returns:
        str: Formatted preview text
    """
    if not text:
        return ""
    
    preview = text[:max_length].strip()
    if len(text) > max_length:
        preview += "..."
    
    return preview


def format_status_emoji(has_attestation):
    """
    Get emoji for attestation status
    
    Args:
        has_attestation: Whether user has submitted attestation
    
    Returns:
        str: Emoji (✅ or ❌)
    """
    return "✅" if has_attestation else "❌"


def format_attachment_indicator(has_attachments, count=0):
    """
    Format attachment indicator
    
    Args:
        has_attachments: Whether there are attachments
        count: Number of attachments
    
    Returns:
        str: Attachment indicator or empty string
    """
    if has_attachments:
        if count > 0:
            return f"📎 ({count})"
        return "📎"
    return ""



