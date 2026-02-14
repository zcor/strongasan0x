from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import logout
from django.http import JsonResponse
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from collections import defaultdict
from django.templatetags.static import static
import time
from .models import (
    WeeklyRollCall, RollCallRanking,
    DiscordUserMapping, TelegramUserMapping, Attestation
)
from rollcall.utils.rollcalls import get_active_roll_call


def get_week_monday(date):
    """Get the Monday of the week containing the given date"""
    from datetime import timedelta
    # Monday is weekday 0, so subtract the weekday to get Monday
    return date - timedelta(days=date.weekday())


def landing(request):
    """Public rankings page - The Roll Call of the Crypto Phalanx"""
    from datetime import date, timedelta
    from django.utils.dateparse import parse_date

    # Get date parameter from query string (format: YYYY-MM-DD)
    date_param = request.GET.get('date')
    requested_week_start = None

    if date_param:
        try:
            parsed_date = parse_date(date_param)
            if parsed_date:
                # Find the Monday of the week containing the requested date
                requested_week_start = get_week_monday(parsed_date)
        except (ValueError, TypeError):
            requested_week_start = None

    # --- Logic for determining which WeeklyRollCall to display ---
    weekly_roll_call = None

    if requested_week_start:
        # If a specific week is requested via parameter, try to find it ONLY if published
        weekly_roll_call = WeeklyRollCall.objects.filter(
            week_start_date=requested_week_start,
            is_published=True
        ).order_by('-week_start_date').first()

    # If no published week was specifically requested or found, default to the most recently PUBLISHED one
    if not weekly_roll_call:
        weekly_roll_call = WeeklyRollCall.objects.filter(is_published=True).order_by('-week_start_date').first()

    # If still no published roll call (meaning no weeks have ever been published),
    # then check for any unpublished current week to show an empty state or message.
    if not weekly_roll_call:
        today = date.today()
        current_calendar_week_monday = get_week_monday(today)
        weekly_roll_call = WeeklyRollCall.objects.filter(
            week_start_date=current_calendar_week_monday,
            is_published=False
        ).first()

    # Final fallback: if still no roll call, get the very last created one
    if not weekly_roll_call:
        weekly_roll_call = WeeklyRollCall.objects.order_by('-week_start_date').first()

    # Determine week_start and week_end based on the found roll_call or a default for display
    if weekly_roll_call:
        week_start = weekly_roll_call.week_start_date
        week_end = weekly_roll_call.week_end_date
    else:
        # Fallback if no roll calls exist at all
        today = date.today()
        week_start = get_week_monday(today)
        week_end = week_start + timedelta(days=6)

    # Fetch rankings for the determined week (only if a roll_call object was found and it's published)
    rankings = []
    if weekly_roll_call and weekly_roll_call.is_published:
        rankings_queryset = RollCallRanking.objects.filter(weekly_roll_call=weekly_roll_call).order_by('rank')
        rankings = list(rankings_queryset)
        for ranking in rankings:
            # Enrich rankings with Discord mapping data (linked_name and linked_twitter_handle)
            discord_mapping = None

            if ranking.name:
                discord_mapping = DiscordUserMapping.objects.filter(
                    linked_name__iexact=ranking.name,
                    is_active=True
                ).first()

                if not discord_mapping:
                    ranking_clean = ranking.name.lower().replace(' ', '').replace('_', '').replace('|', '').replace('-', '').replace('of', '').replace('house', '')
                    for mapping in DiscordUserMapping.objects.filter(is_active=True):
                        if mapping.linked_name:
                            linked_clean = mapping.linked_name.lower().replace(' ', '').replace('_', '').replace('|', '').replace('-', '').replace('of', '').replace('house', '')
                            if ranking_clean == linked_clean or ranking_clean in linked_clean or linked_clean in ranking_clean:
                                discord_mapping = mapping
                                break

            if not discord_mapping and ranking.twitter_handle:
                discord_mapping = DiscordUserMapping.objects.filter(
                    linked_twitter_handle__iexact=ranking.twitter_handle,
                    is_active=True
                ).first()

            if discord_mapping:
                ranking.display_name = discord_mapping.linked_name or ranking.name
                ranking.twitter_handle = discord_mapping.linked_twitter_handle or ranking.twitter_handle
            else:
                ranking.display_name = ranking.name

    # Calculate previous and next week dates for navigation, considering only PUBLISHED weeks
    previous_week_roll_call = None
    if weekly_roll_call:
        previous_week_roll_call = WeeklyRollCall.objects.filter(
            week_start_date__lt=week_start,
            is_published=True
        ).order_by('-week_start_date').first()

    next_week_roll_call = None
    if weekly_roll_call:
        next_week_roll_call = WeeklyRollCall.objects.filter(
            week_start_date__gt=week_start,
            is_published=True
        ).order_by('week_start_date').first()

    previous_week_date = previous_week_roll_call.week_start_date.strftime('%Y-%m-%d') if previous_week_roll_call else None
    next_week_date = next_week_roll_call.week_start_date.strftime('%Y-%m-%d') if next_week_roll_call else None

    # Build video URL with cache-busting timestamp
    video_url = static('rollcall/videos/vid1.mp4')
    video_url_with_timestamp = f"{video_url}?v={int(time.time())}"

    context = {
        'timestamp': int(time.time()),
        'video_url': video_url_with_timestamp,
        'weekly_roll_call': weekly_roll_call,
        'rankings': rankings,
        'previous_week_date': previous_week_date,
        'next_week_date': next_week_date,
        'current_week_start': week_start,
        'current_week_end': week_end,
        'publication_date': week_end + timedelta(days=1),  # Monday after week ends
        'has_previous_week': bool(previous_week_roll_call),
        'has_next_week': bool(next_week_roll_call),
    }
    return render(request, 'rollcall/landing.html', context)


# Telegram webhook handler
try:
    from rollcall.telegram_bot.handlers.webhook import telegram_webhook as telegram_webhook_handler
except ImportError:
    # Telegram bot not available (python-telegram-bot not installed)
    def telegram_webhook_handler(request):
        from django.http import JsonResponse
        return JsonResponse({'ok': False, 'error': 'Telegram bot not configured'}, status=503)


def telegram_webhook(request):
    """Telegram webhook endpoint"""
    return telegram_webhook_handler(request)


# Web authentication and account management
def login_view(request):
    """Login page with Discord and Telegram OAuth options"""
    if request.user.is_authenticated:
        return redirect('account')

    return render(request, 'rollcall/login.html')


def logout_view(request):
    """Logout user"""
    logout(request)
    return redirect('landing')


@login_required
def account(request):
    """Account management page - view/manage attestations and linked accounts"""
    # Get linked accounts
    discord_mapping = DiscordUserMapping.objects.filter(user=request.user, is_active=True).first()
    telegram_mapping = TelegramUserMapping.objects.filter(user=request.user, is_active=True).first()

    # Get active roll call (Monday mapped to prior week to catch stragglers)
    current_roll_call = get_active_roll_call(allow_create=False)
    if not current_roll_call:
        current_roll_call = WeeklyRollCall.objects.order_by('-week_start_date').first()

    # Get user's attestations
    user_attestations = []
    if discord_mapping:
        user_attestations.extend(
            Attestation.objects.filter(
                discord_user=discord_mapping,
                parent_attestation__isnull=True
            ).order_by('-posted_at')[:10]
        )
    if telegram_mapping:
        user_attestations.extend(
            Attestation.objects.filter(
                telegram_user=telegram_mapping,
                parent_attestation__isnull=True
            ).order_by('-posted_at')[:10]
        )

    # Sort by posted_at
    user_attestations.sort(key=lambda x: x.posted_at, reverse=True)

    # Get current week attestation status
    current_week_attestation = None
    if current_roll_call:
        if discord_mapping:
            current_week_attestation = Attestation.objects.filter(
                weekly_roll_call=current_roll_call,
                discord_user=discord_mapping,
                parent_attestation__isnull=True
            ).first()
        if not current_week_attestation and telegram_mapping:
            current_week_attestation = Attestation.objects.filter(
                weekly_roll_call=current_roll_call,
                telegram_user=telegram_mapping,
                parent_attestation__isnull=True
            ).first()

    context = {
        'discord_mapping': discord_mapping,
        'telegram_mapping': telegram_mapping,
        'current_roll_call': current_roll_call,
        'current_week_attestation': current_week_attestation,
        'user_attestations': user_attestations[:10],  # Last 10
    }
    return render(request, 'rollcall/account.html', context)


@login_required
@require_http_methods(["POST"])
def link_discord(request):
    """Link Discord account to user"""
    discord_user_id = request.POST.get('discord_user_id')
    if not discord_user_id:
        messages.error(request, "Discord user ID required")
        return redirect('account')

    try:
        mapping = DiscordUserMapping.objects.get(discord_user_id=int(discord_user_id))
        mapping.user = request.user
        mapping.save()
        messages.success(request, "Discord account linked successfully")
    except DiscordUserMapping.DoesNotExist:
        messages.error(request, "Discord account not found")
    except Exception as e:
        messages.error(request, f"Error linking Discord account: {e}")

    return redirect('account')


@login_required
@require_http_methods(["POST"])
def link_telegram(request):
    """Link Telegram account to user"""
    telegram_user_id = request.POST.get('telegram_user_id')
    if not telegram_user_id:
        messages.error(request, "Telegram user ID required")
        return redirect('account')

    try:
        mapping = TelegramUserMapping.objects.get(telegram_user_id=int(telegram_user_id))
        mapping.user = request.user
        mapping.save()
        messages.success(request, "Telegram account linked successfully")
    except TelegramUserMapping.DoesNotExist:
        messages.error(request, "Telegram account not found")
    except Exception as e:
        messages.error(request, f"Error linking Telegram account: {e}")

    return redirect('account')


@login_required
@require_http_methods(["POST"])
def unlink_discord(request):
    """Unlink Discord account from user"""
    try:
        mapping = DiscordUserMapping.objects.get(user=request.user)
        mapping.user = None
        mapping.save()
        messages.success(request, "Discord account unlinked successfully")
    except DiscordUserMapping.DoesNotExist:
        messages.error(request, "No Discord account linked")
    except Exception as e:
        messages.error(request, f"Error unlinking Discord account: {e}")

    return redirect('account')


@login_required
@require_http_methods(["POST"])
def unlink_telegram(request):
    """Unlink Telegram account from user"""
    try:
        mapping = TelegramUserMapping.objects.get(user=request.user)
        mapping.user = None
        mapping.save()
        messages.success(request, "Telegram account unlinked successfully")
    except TelegramUserMapping.DoesNotExist:
        messages.error(request, "No Telegram account linked")
    except Exception as e:
        messages.error(request, f"Error unlinking Telegram account: {e}")

    return redirect('account')


# =============================================================================
# Attestation Review Interface (Admin Only)
# =============================================================================

@staff_member_required
def review_attestations(request):
    """
    Admin-only interface for reviewing weekly attestations.
    Shows attestations grouped by user with collapsible sections.
    """
    from datetime import date

    # Get available weeks (most recent first)
    available_weeks = WeeklyRollCall.objects.order_by('-week_end_date')[:20]

    # Get selected week from query param, default to most recent
    week_param = request.GET.get('week')
    if week_param:
        try:
            week_end_date = date.fromisoformat(week_param)
            current_week = WeeklyRollCall.objects.filter(week_end_date=week_end_date).first()
        except ValueError:
            current_week = available_weeks.first() if available_weeks else None
    else:
        current_week = available_weeks.first() if available_weeks else None

    # Get attestations for the selected week
    attestations_by_user = defaultdict(list)
    total_count = 0
    hidden_count = 0

    if current_week:
        attestations = Attestation.objects.filter(
            weekly_roll_call=current_week,
            parent_attestation__isnull=True  # Only parent attestations
        ).select_related(
            'discord_user', 'telegram_user'
        ).prefetch_related('parts').order_by('posted_at')

        for att in attestations:
            # Determine user name
            if att.discord_user and att.discord_user.linked_name:
                user_name = att.discord_user.linked_name
            elif att.telegram_user and att.telegram_user.linked_name:
                user_name = att.telegram_user.linked_name
            elif att.discord_user:
                user_name = f"Discord: {att.discord_user.discord_username}"
            elif att.telegram_user:
                user_name = f"Telegram: {att.telegram_user.telegram_first_name}"
            else:
                user_name = "Unknown User"

            attestations_by_user[user_name].append(att)
            total_count += 1
            if att.is_hidden:
                hidden_count += 1

    # Sort by user name
    attestations_by_user = dict(sorted(attestations_by_user.items()))

    # Get all users for the add attestation dropdown
    discord_users = DiscordUserMapping.objects.filter(
        is_active=True
    ).exclude(linked_name='').exclude(linked_name__isnull=True).order_by('linked_name')

    telegram_users = TelegramUserMapping.objects.filter(
        is_active=True
    ).exclude(linked_name='').exclude(linked_name__isnull=True).order_by('linked_name')

    context = {
        'available_weeks': available_weeks,
        'current_week': current_week,
        'attestations_by_user': attestations_by_user,
        'total_count': total_count,
        'hidden_count': hidden_count,
        'active_count': total_count - hidden_count,
        'participant_count': len(attestations_by_user),
        'discord_users': discord_users,
        'telegram_users': telegram_users,
    }

    return render(request, 'rollcall/review_attestations.html', context)


@staff_member_required
@require_http_methods(["POST"])
def toggle_attestation_hidden(request, attestation_id):
    """Toggle the is_hidden status of an attestation (AJAX endpoint)"""
    attestation = get_object_or_404(Attestation, id=attestation_id)

    # Toggle hidden status
    attestation.is_hidden = not attestation.is_hidden
    attestation.hidden_at = timezone.now() if attestation.is_hidden else None
    attestation.save()

    return JsonResponse({
        'success': True,
        'is_hidden': attestation.is_hidden,
        'attestation_id': attestation_id,
    })


@staff_member_required
@require_http_methods(["POST"])
def add_manual_attestation(request):
    """Add a manual attestation for a user"""

    user_type = request.POST.get('user_type')  # 'discord' or 'telegram'
    user_id = request.POST.get('user_id')
    text = request.POST.get('text', '').strip()
    week_id = request.POST.get('week_id')

    if not all([user_type, user_id, text, week_id]):
        messages.error(request, "All fields are required")
        return redirect('review_attestations')

    try:
        week = WeeklyRollCall.objects.get(id=week_id)
    except WeeklyRollCall.DoesNotExist:
        messages.error(request, "Invalid week selected")
        return redirect('review_attestations')

    # Get user mapping
    discord_user = None
    telegram_user = None

    if user_type == 'discord':
        try:
            discord_user = DiscordUserMapping.objects.get(id=user_id)
        except DiscordUserMapping.DoesNotExist:
            messages.error(request, "Discord user not found")
            return redirect('review_attestations')
    else:
        try:
            telegram_user = TelegramUserMapping.objects.get(id=user_id)
        except TelegramUserMapping.DoesNotExist:
            messages.error(request, "Telegram user not found")
            return redirect('review_attestations')

    # Create the attestation
    Attestation.objects.create(
        weekly_roll_call=week,
        source=user_type,
        discord_user=discord_user,
        telegram_user=telegram_user,
        raw_text=text,
        posted_at=timezone.now(),
        parsed_data={'manually_added': True, 'added_by': request.user.username}
    )

    user_name = (discord_user.linked_name if discord_user else
                 telegram_user.linked_name if telegram_user else "Unknown")
    messages.success(request, f"Attestation added for {user_name}")

    return redirect(f"{request.path.replace('/add/', '/')}?week={week.week_end_date}")
