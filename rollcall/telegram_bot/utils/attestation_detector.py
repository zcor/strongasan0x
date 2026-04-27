"""
Attestation detection logic for Telegram messages.
Based on analysis of chat history, detects if a message is likely an attestation.
"""
import re
from django.conf import settings
from rollcall.utils.timezone_utils import is_in_attestation_window


# Keywords that indicate an attestation
# NOTE: Bare 'attestation' removed - it's meta-discussion, not proof of fitness
ATTESTATION_KEYWORDS = [
    'steps', 'miles', 'sets', 'hours', 'reps', 'training', 'workout', 'exercise',
    'running', 'lifting', 'gym', 'strength', 'cardio', 'calories', 'distance',
    'weekly', 'this week', 'week of', 'sunday', 'monday', 'tuesday',
    'wednesday', 'thursday', 'friday', 'saturday', 'push-up', 'pull-up', 'squat',
    'plank', 'crunch', 'deadlift', 'curl', 'lunges', 'bjj', 'striking', 'grappling',
    'murph', 'jump rope', 'skipping', 'hike', 'mountain', 'beach', 'vest'
]

# Intent markers that strongly indicate an attestation
# NOTE: 'attestation' now requires context (my/weekly/health attestation)
INTENT_MARKERS = [
    r'(?:my|weekly|health)\s+attestation',  # Contextual attestation phrases only
    r'weekly\s+health',
    r'week\s+of',
    r'here\'?s?\s+my',
    r'this\s+week',
    r'week\s+ending',
]

# Conversational patterns that indicate NOT an attestation
CONVERSATIONAL_PATTERNS = [
    r'^@\w+',                          # Starts with @mention
    r'can you|could you|would you',    # Request phrases
    r'how do|how does|how to',         # How-to questions
    r'help me|show me|tell me',        # Help requests
    r'does (?:it|this|that)\s+work',   # Tech support questions
    r'what (?:is|are)\s+the',          # What questions
]


def is_weekend_message(message_date):
    """
    Check if message is within attestation window.
    Window: Friday 5 PM Pacific through Monday 6 PM Pacific.

    Args:
        message_date: datetime object (will be converted to Pacific internally)

    Returns:
        bool: True if message is within attestation window
    """
    return is_in_attestation_window(message_date)


def has_attestation_structure(text):
    """
    Check if text has structured format typical of attestations

    Args:
        text: Message text

    Returns:
        bool: True if text appears structured
    """
    if not text:
        return False

    # Check for bullet points or numbered lists
    if re.search(r'^[\-\*•]\s+', text, re.MULTILINE):
        return True

    # Check for numbered lists
    if re.search(r'^\d+[\.\)]\s+', text, re.MULTILINE):
        return True

    # Check for daily breakdown (e.g., "Sunday 16th", "Monday 17th")
    if re.search(r'(?:sun|mon|tues|wednes|thurs|fri|satur)day\s+\d+', text, re.IGNORECASE):
        return True

    # Check for day-prefixed lines (e.g., "Monday:", "Tuesday:", "Mon:", "Tue:")
    # Also match bare day names on their own line (e.g., "Tuesday\nLegs\n...")
    if re.search(r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*:?$', text, re.MULTILINE | re.IGNORECASE):
        return True
    if re.search(r'^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s*:?$', text, re.MULTILINE | re.IGNORECASE):
        return True

    # Check for markdown headers
    if re.search(r'^#+\s+', text, re.MULTILINE):
        return True

    # Check for structured sections (Summary, etc.)
    if re.search(r'^##?\s+(?:summary|sunday|monday|tuesday|wednesday|thursday|friday|saturday)', text, re.MULTILINE | re.IGNORECASE):
        return True

    return False


def has_metrics(text):
    """
    Check if text contains metrics (numbers with units)
    
    Args:
        text: Message text
    
    Returns:
        bool: True if text contains metrics
    """
    if not text:
        return False
    
    # Patterns for metrics: number + unit
    metric_patterns = [
        r'\d+\s*(?:steps|miles|km|kilometers|hours?|mins?|minutes?|sets?|reps?|lbs?|kg|pounds?|calories?)',
        r'\d+k\s*(?:steps?)',  # e.g., "13k steps"
        r'\d+\.\d+\s*(?:miles?|km|hours?)',  # Decimal numbers
        r'\d+:\d+',  # Time format (e.g., "13:50")
        r'\d{2,3}-\d{1,2}(?:,\s*\d{2,3}-\d{1,2})',  # Weight-reps notation (e.g., "225-3, 245-1")
    ]
    
    for pattern in metric_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    
    return False


def has_attestation_keywords(text):
    """
    Check if text contains attestation-related keywords
    
    Args:
        text: Message text
    
    Returns:
        bool: True if text contains relevant keywords
    """
    if not text:
        return False
    
    text_lower = text.lower()
    
    # Check for keywords
    for keyword in ATTESTATION_KEYWORDS:
        if keyword in text_lower:
            return True
    
    return False


def has_intent_markers(text):
    """
    Check if text contains explicit intent markers

    Args:
        text: Message text

    Returns:
        bool: True if text contains intent markers
    """
    if not text:
        return False

    text_lower = text.lower()

    for marker in INTENT_MARKERS:
        if re.search(marker, text_lower):
            return True

    return False


def has_conversational_markers(text):
    """
    Check if text contains conversational patterns that indicate
    it's NOT an attestation (e.g., questions, requests, @mentions)

    Args:
        text: Message text

    Returns:
        bool: True if text appears conversational (not an attestation)
    """
    if not text:
        return False

    text_lower = text.lower()

    for pattern in CONVERSATIONAL_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            return True

    return False


def is_likely_attestation(text, message_date=None, min_length=None):
    """
    Determine if a message is likely an attestation based on heuristics

    Args:
        text: Message text
        message_date: datetime object (optional, for weekend detection)
        min_length: Minimum length threshold (defaults to setting or 100)

    Returns:
        bool: True if message is likely an attestation
    """
    if not text:
        return False

    # Get minimum length from settings or use default
    if min_length is None:
        min_length = getattr(settings, 'ATTESTATION_MIN_LENGTH', 100)

    # Length check
    if len(text.strip()) < min_length:
        return False

    # Weekend check (if date provided)
    if message_date and not is_weekend_message(message_date):
        return False

    # Check for substance (structure or metrics)
    has_structure = has_attestation_structure(text)
    has_metric = has_metrics(text)
    has_substance = has_structure or has_metric

    # Score-based detection
    score = 0

    # Strong indicators (worth 2 points each)
    if has_intent_markers(text):
        score += 2

    if has_structure:
        score += 2

    # Medium indicators
    if has_metric:
        score += 2  # INCREASED from 1 to 2

    if has_attestation_keywords(text):
        score += 1

    # Conversational penalty: only apply if no substance
    if has_conversational_markers(text) and not has_substance:
        score -= 1

    # Threshold logic:
    # - Weekend with substance: threshold 1 (lenient for real attestations)
    # - Weekend without substance: threshold 2 (stricter for keywords-only)
    # - Non-weekend: threshold 2
    if message_date and is_weekend_message(message_date):
        threshold = 1 if has_substance else 2
    else:
        threshold = 2

    return score >= threshold



