"""
Warrior Dashboard views.
Public-facing dashboard for warriors to view/edit their attestations.
Uses Telegram bot deep linking for authentication (NOT Django auth).
"""
import logging
from django.shortcuts import render, redirect
from django.http import HttpResponseBadRequest
from django.views.decorators.http import require_http_methods
from django.conf import settings
from django.utils import timezone

from rollcall.models import TelegramUserMapping, Attestation, WebLoginToken
from rollcall.utils.rollcalls import get_active_roll_call

from .auth import (
    verify_telegram_auth,
    get_telegram_user_from_session,
    set_telegram_session,
    clear_telegram_session,
    require_telegram_auth,
    SESSION_TELEGRAM_FIRST_NAME,
    SESSION_TELEGRAM_USER_ID,
)

logger = logging.getLogger(__name__)


def warrior_index(request):
    """Redirect to dashboard if authenticated, otherwise to login."""
    if get_telegram_user_from_session(request):
        return redirect('warrior:dashboard')
    return redirect('warrior:login')


def warrior_login(request):
    """Display token-based login via Telegram bot."""
    if get_telegram_user_from_session(request):
        return redirect('warrior:dashboard')

    bot_username = getattr(settings, 'TELEGRAM_BOT_USERNAME', '')

    # Create a new login token
    login_token = WebLoginToken.create_token()
    logger.info(f"Created login token: {login_token.token}")

    context = {
        'telegram_bot_username': bot_username,
        'login_token': login_token.token,
    }
    return render(request, 'rollcall/warrior/login.html', context)


def check_login(request):
    """Check if a login token has been confirmed via Telegram bot."""
    token_str = request.GET.get('token', '')

    if not token_str:
        return redirect('warrior:login')

    try:
        token = WebLoginToken.objects.get(token=token_str)
    except WebLoginToken.DoesNotExist:
        logger.warning(f"Invalid login token: {token_str}")
        return render(request, 'rollcall/warrior/login_error.html', {
            'error': 'Invalid login token. Please try again.',
            'telegram_bot_username': getattr(settings, 'TELEGRAM_BOT_USERNAME', ''),
        })

    if not token.is_valid():
        logger.warning(f"Expired login token: {token_str}")
        return render(request, 'rollcall/warrior/login_error.html', {
            'error': 'Login token has expired. Please try again.',
            'telegram_bot_username': getattr(settings, 'TELEGRAM_BOT_USERNAME', ''),
        })

    if not token.telegram_user:
        # Token not yet confirmed - show waiting message
        return render(request, 'rollcall/warrior/login_pending.html', {
            'token': token_str,
            'telegram_bot_username': getattr(settings, 'TELEGRAM_BOT_USERNAME', ''),
        })

    # Token is confirmed - log the user in
    token.is_used = True
    token.save()

    # Create auth_data for session
    auth_data = {
        'id': str(token.telegram_user.telegram_user_id),
        'username': token.telegram_user.telegram_username,
        'first_name': token.telegram_user.telegram_first_name,
        'last_name': token.telegram_user.telegram_last_name,
    }
    set_telegram_session(request, auth_data)

    logger.info(f"User logged in via token: {token.telegram_user.telegram_user_id}")
    return redirect('warrior:dashboard')


def telegram_callback(request):
    """Handle Telegram Login Widget callback (legacy, may not be used)."""
    logger.info(f"Telegram callback hit. GET params: {dict(request.GET)}")

    # Widget sends data via GET parameters
    auth_data = {
        'id': request.GET.get('id'),
        'first_name': request.GET.get('first_name', ''),
        'last_name': request.GET.get('last_name', ''),
        'username': request.GET.get('username', ''),
        'photo_url': request.GET.get('photo_url', ''),
        'auth_date': request.GET.get('auth_date'),
        'hash': request.GET.get('hash'),
    }

    logger.info(f"Auth data extracted: id={auth_data['id']}, username={auth_data['username']}")

    if not verify_telegram_auth(auth_data):
        logger.warning(f"Auth verification failed for data: {auth_data}")
        return HttpResponseBadRequest('Invalid authentication')

    # Check auth_date is recent (within 1 hour)
    try:
        auth_timestamp = int(auth_data['auth_date'])
        current_timestamp = int(timezone.now().timestamp())
        if current_timestamp - auth_timestamp > 3600:
            return HttpResponseBadRequest('Authentication expired')
    except (ValueError, TypeError):
        return HttpResponseBadRequest('Invalid auth_date')

    telegram_user_id = int(auth_data['id'])

    # Get or create TelegramUserMapping
    mapping, created = TelegramUserMapping.objects.get_or_create(
        telegram_user_id=telegram_user_id,
        defaults={
            'telegram_username': auth_data.get('username', ''),
            'telegram_first_name': auth_data.get('first_name', ''),
            'telegram_last_name': auth_data.get('last_name', ''),
        }
    )

    # Update mapping if it already exists (in case username/name changed)
    if not created:
        updated = False
        if auth_data.get('username') and auth_data['username'] != mapping.telegram_username:
            mapping.telegram_username = auth_data['username']
            updated = True
        if auth_data.get('first_name') and auth_data['first_name'] != mapping.telegram_first_name:
            mapping.telegram_first_name = auth_data['first_name']
            updated = True
        if auth_data.get('last_name') and auth_data['last_name'] != mapping.telegram_last_name:
            mapping.telegram_last_name = auth_data['last_name']
            updated = True
        if updated:
            mapping.save()

    # Store in session
    set_telegram_session(request, auth_data)

    return redirect('warrior:dashboard')


def warrior_logout(request):
    """Clear Telegram session and redirect to login."""
    clear_telegram_session(request)
    return redirect('warrior:login')


@require_telegram_auth
def warrior_dashboard(request):
    """Main dashboard showing current week status."""
    from rollcall.models import StravaAuth

    telegram_user_id = request.session.get(SESSION_TELEGRAM_USER_ID)
    mapping = TelegramUserMapping.objects.filter(telegram_user_id=telegram_user_id).first()

    current_roll_call = get_active_roll_call(allow_create=False)
    current_attestation = None

    if current_roll_call and mapping:
        # Get the main attestation (not a multi-part child)
        current_attestation = Attestation.objects.filter(
            weekly_roll_call=current_roll_call,
            telegram_user=mapping,
            parent_attestation__isnull=True
        ).first()

    # Check for Strava integration
    strava_auth = None
    if mapping:
        strava_auth = StravaAuth.objects.filter(telegram_user=mapping).first()

    context = {
        'telegram_user': mapping,
        'current_roll_call': current_roll_call,
        'current_attestation': current_attestation,
        'telegram_first_name': request.session.get(SESSION_TELEGRAM_FIRST_NAME, ''),
        'strava_auth': strava_auth,
    }
    return render(request, 'rollcall/warrior/dashboard.html', context)


@require_telegram_auth
def attestation_history(request):
    """View past attestations by week."""
    telegram_user_id = request.session.get(SESSION_TELEGRAM_USER_ID)
    mapping = TelegramUserMapping.objects.filter(telegram_user_id=telegram_user_id).first()

    attestations = []
    if mapping:
        # Get all main attestations (not multi-part children), ordered by date
        attestations = Attestation.objects.filter(
            telegram_user=mapping,
            parent_attestation__isnull=True
        ).select_related('weekly_roll_call').order_by('-posted_at')

    context = {
        'attestations': attestations,
        'telegram_first_name': request.session.get(SESSION_TELEGRAM_FIRST_NAME, ''),
    }
    return render(request, 'rollcall/warrior/history.html', context)


@require_telegram_auth
@require_http_methods(["GET", "POST"])
def edit_attestation(request):
    """Edit current week's attestation."""
    telegram_user_id = request.session.get(SESSION_TELEGRAM_USER_ID)
    mapping = TelegramUserMapping.objects.filter(telegram_user_id=telegram_user_id).first()

    if not mapping:
        return redirect('warrior:login')

    current_roll_call = get_active_roll_call(allow_create=True)
    if not current_roll_call:
        return redirect('warrior:dashboard')

    # Get existing attestation if any
    attestation = Attestation.objects.filter(
        weekly_roll_call=current_roll_call,
        telegram_user=mapping,
        parent_attestation__isnull=True
    ).first()

    if request.method == 'POST':
        raw_text = request.POST.get('raw_text', '').strip()

        if attestation:
            # Update existing attestation
            attestation.raw_text = raw_text
            # Note the source in parsed_data
            parsed_data = attestation.parsed_data or {}
            parsed_data['submitted_via'] = 'warrior_dashboard'
            parsed_data['last_edited'] = timezone.now().isoformat()
            attestation.parsed_data = parsed_data
            attestation.save()
        else:
            # Create new attestation
            attestation = Attestation.objects.create(
                weekly_roll_call=current_roll_call,
                source='telegram',
                telegram_user=mapping,
                raw_text=raw_text,
                posted_at=timezone.now(),
                parsed_data={'submitted_via': 'warrior_dashboard'},
            )

        return redirect('warrior:dashboard')

    context = {
        'attestation': attestation,
        'current_roll_call': current_roll_call,
        'telegram_first_name': request.session.get(SESSION_TELEGRAM_FIRST_NAME, ''),
    }
    return render(request, 'rollcall/warrior/edit.html', context)


# =============================================================================
# Strava Integration Views
# =============================================================================

@require_telegram_auth
def link_strava(request):
    """Redirect user to Strava OAuth authorization."""
    import secrets
    from rollcall.services.strava_client import StravaClient

    # Generate state token for CSRF protection
    state = secrets.token_urlsafe(32)
    request.session['strava_oauth_state'] = state

    # Build callback URL
    callback_url = request.build_absolute_uri('/warrior/strava/callback/')

    auth_url = StravaClient.get_authorization_url(callback_url, state)
    logger.info(f"Redirecting to Strava OAuth: {auth_url}")

    return redirect(auth_url)


@require_telegram_auth
def strava_callback(request):
    """Handle Strava OAuth callback."""
    from rollcall.models import StravaAuth
    from rollcall.services.strava_client import StravaClient

    code = request.GET.get('code')
    state = request.GET.get('state')
    error = request.GET.get('error')

    # Check for error from Strava
    if error:
        logger.warning(f"Strava OAuth error: {error}")
        return render(request, 'rollcall/warrior/strava_error.html', {
            'error': f'Strava authorization failed: {error}',
            'telegram_first_name': request.session.get(SESSION_TELEGRAM_FIRST_NAME, ''),
        })

    # Validate state token
    expected_state = request.session.pop('strava_oauth_state', None)
    if not expected_state or state != expected_state:
        logger.warning("Strava OAuth state mismatch")
        return render(request, 'rollcall/warrior/strava_error.html', {
            'error': 'Security validation failed. Please try again.',
            'telegram_first_name': request.session.get(SESSION_TELEGRAM_FIRST_NAME, ''),
        })

    if not code:
        logger.warning("No authorization code in Strava callback")
        return render(request, 'rollcall/warrior/strava_error.html', {
            'error': 'No authorization code received. Please try again.',
            'telegram_first_name': request.session.get(SESSION_TELEGRAM_FIRST_NAME, ''),
        })

    # Get the telegram user mapping
    telegram_user_id = request.session.get(SESSION_TELEGRAM_USER_ID)
    mapping = TelegramUserMapping.objects.filter(telegram_user_id=telegram_user_id).first()

    if not mapping:
        return redirect('warrior:login')

    try:
        # Exchange code for tokens
        client = StravaClient()
        token_data = client.exchange_code(code)

        # Create or update StravaAuth
        strava_auth, created = StravaAuth.objects.update_or_create(
            telegram_user=mapping,
            defaults={
                'athlete_id': token_data['athlete'].get('id'),
                'access_token': token_data['access_token'],
                'refresh_token': token_data['refresh_token'],
                'token_expires_at': token_data['expires_at'],
                'athlete_data': token_data['athlete'],
            }
        )

        action = "linked" if created else "updated"
        athlete_name = token_data['athlete'].get('firstname', 'Unknown')
        logger.info(f"Strava account {action} for {mapping}: athlete {athlete_name}")

    except Exception as e:
        logger.error(f"Failed to complete Strava OAuth: {e}")
        return render(request, 'rollcall/warrior/strava_error.html', {
            'error': f'Failed to link Strava account: {str(e)}',
            'telegram_first_name': request.session.get(SESSION_TELEGRAM_FIRST_NAME, ''),
        })

    return redirect('warrior:dashboard')


@require_telegram_auth
@require_http_methods(["POST"])
def unlink_strava(request):
    """Remove Strava account link."""
    from rollcall.models import StravaAuth

    telegram_user_id = request.session.get(SESSION_TELEGRAM_USER_ID)
    mapping = TelegramUserMapping.objects.filter(telegram_user_id=telegram_user_id).first()

    if mapping:
        deleted, _ = StravaAuth.objects.filter(telegram_user=mapping).delete()
        if deleted:
            logger.info(f"Unlinked Strava account for {mapping}")

    return redirect('warrior:dashboard')


@require_telegram_auth
def strava_attestation(request):
    """Generate weekly attestation from Strava activities."""
    from datetime import timedelta
    from rollcall.models import StravaAuth, StravaActivity
    from rollcall.services.strava_client import StravaClient

    telegram_user_id = request.session.get(SESSION_TELEGRAM_USER_ID)
    mapping = TelegramUserMapping.objects.filter(telegram_user_id=telegram_user_id).first()

    if not mapping:
        return redirect('warrior:login')

    # Get Strava auth
    try:
        strava_auth = StravaAuth.objects.get(telegram_user=mapping)
    except StravaAuth.DoesNotExist:
        return redirect('warrior:dashboard')

    # Calculate week range (Monday to Sunday)
    today = timezone.now().date()
    # Find the most recent Monday (or today if it's Monday)
    days_since_monday = today.weekday()
    week_start = today - timedelta(days=days_since_monday)
    week_end = week_start + timedelta(days=6)

    # Convert to datetime with timezone
    week_start_dt = timezone.make_aware(
        timezone.datetime.combine(week_start, timezone.datetime.min.time())
    )
    week_end_dt = timezone.make_aware(
        timezone.datetime.combine(week_end, timezone.datetime.max.time())
    )

    # Sync activities from Strava
    try:
        client = StravaClient(strava_auth)
        created, updated = client.sync_activities(after=week_start_dt, before=week_end_dt)
        sync_message = f"Synced {created + updated} activities from Strava."
    except Exception as e:
        logger.error(f"Failed to sync Strava activities: {e}")
        sync_message = f"Warning: Could not sync latest activities: {str(e)}"

    # Get activities for the week
    activities = StravaActivity.objects.filter(
        strava_auth=strava_auth,
        start_date__gte=week_start_dt,
        start_date__lte=week_end_dt,
    ).order_by('start_date')

    # Calculate weekly totals
    total_moving_time = sum(a.moving_time_seconds for a in activities)
    total_distance = sum(a.distance_meters or 0 for a in activities)
    total_elevation = sum(a.total_elevation_gain or 0 for a in activities)

    # Format totals
    total_hours = total_moving_time // 3600
    total_minutes = (total_moving_time % 3600) // 60
    total_distance_miles = total_distance / 1609.344
    total_elevation_feet = total_elevation * 3.28084

    # Get athlete name
    athlete_name = strava_auth.athlete_data.get('firstname', '') or 'Athlete'

    context = {
        'telegram_first_name': request.session.get(SESSION_TELEGRAM_FIRST_NAME, ''),
        'athlete_name': athlete_name,
        'week_start': week_start,
        'week_end': week_end,
        'activities': activities,
        'activity_count': activities.count(),
        'total_time_formatted': f"{total_hours}h {total_minutes}m",
        'total_distance_miles': round(total_distance_miles, 1),
        'total_elevation_feet': round(total_elevation_feet),
        'sync_message': sync_message,
    }
    return render(request, 'rollcall/warrior/strava_attestation.html', context)
