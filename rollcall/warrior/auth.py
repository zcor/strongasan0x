"""
Telegram Login Widget authentication utilities.
Handles hash verification and session management for the Warrior Dashboard.
"""
import hashlib
import hmac
from functools import wraps
from django.shortcuts import redirect
from django.conf import settings

# Session keys (prefixed to avoid conflicts with Django auth)
SESSION_TELEGRAM_USER_ID = 'warrior_telegram_user_id'
SESSION_TELEGRAM_USERNAME = 'warrior_telegram_username'
SESSION_TELEGRAM_FIRST_NAME = 'warrior_telegram_first_name'
SESSION_TELEGRAM_AUTH_DATE = 'warrior_telegram_auth_date'


def verify_telegram_auth(auth_data: dict) -> bool:
    """
    Verify Telegram Login Widget authentication data.

    The auth_data should contain: id, first_name, last_name, username,
    photo_url, auth_date, hash

    Verification process (per Telegram docs):
    1. Create data-check-string: sorted key=value pairs joined by newlines, excluding 'hash'
    2. Create secret_key = SHA256(bot_token)
    3. Create check_hash = HMAC-SHA256(data_check_string, secret_key)
    4. Compare check_hash with provided hash
    """
    bot_token = getattr(settings, 'TELEGRAM_BOT_TOKEN', None)
    if not bot_token:
        return False

    # Extract hash and validate required fields
    provided_hash = auth_data.get('hash')
    if not provided_hash or not auth_data.get('id') or not auth_data.get('auth_date'):
        return False

    # Build data-check-string (sorted key=value pairs, excluding hash and empty values)
    data_check_arr = []
    for key in sorted(auth_data.keys()):
        if key != 'hash' and auth_data[key]:
            data_check_arr.append(f"{key}={auth_data[key]}")

    data_check_string = '\n'.join(data_check_arr)

    # Create secret key: SHA256 of bot token
    secret_key = hashlib.sha256(bot_token.encode()).digest()

    # Calculate HMAC-SHA256
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(calculated_hash, provided_hash)


def get_telegram_user_from_session(request):
    """Get telegram user ID from session if authenticated."""
    return request.session.get(SESSION_TELEGRAM_USER_ID)


def set_telegram_session(request, auth_data: dict):
    """Store Telegram user info in session."""
    request.session[SESSION_TELEGRAM_USER_ID] = int(auth_data['id'])
    request.session[SESSION_TELEGRAM_USERNAME] = auth_data.get('username', '')
    request.session[SESSION_TELEGRAM_FIRST_NAME] = auth_data.get('first_name', '')
    request.session[SESSION_TELEGRAM_AUTH_DATE] = auth_data.get('auth_date', '')


def clear_telegram_session(request):
    """Clear Telegram session data."""
    for key in [SESSION_TELEGRAM_USER_ID, SESSION_TELEGRAM_USERNAME,
                SESSION_TELEGRAM_FIRST_NAME, SESSION_TELEGRAM_AUTH_DATE]:
        request.session.pop(key, None)


def require_telegram_auth(view_func):
    """Decorator to require Telegram authentication for a view."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not get_telegram_user_from_session(request):
            return redirect('warrior:login')
        return view_func(request, *args, **kwargs)
    return wrapper
