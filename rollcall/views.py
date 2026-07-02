from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import logout
from django.http import JsonResponse, Http404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from collections import defaultdict, Counter
from django.templatetags.static import static
import math
import time
from .models import (
    WeeklyRollCall, RollCallRanking,
    DiscordUserMapping, TelegramUserMapping, Attestation
)
from rollcall.utils.rollcalls import get_active_roll_call
from rollcall.services.roll_call_markdown import render_roll_call_markdown


def get_week_monday(date):
    """Get the Monday of the week containing the given date"""
    from datetime import timedelta
    # Monday is weekday 0, so subtract the weekday to get Monday
    return date - timedelta(days=date.weekday())


def _clean_name(name):
    """Fuzzy cleanup for name matching: lowercase, strip spaces/underscores/pipes/dashes/of/house."""
    if not name:
        return ''
    return name.lower().replace(' ', '').replace('_', '').replace('|', '').replace('-', '').replace('of', '').replace('house', '')


def _build_mapping_indices(discord_mappings, telegram_mappings):
    """Build in-memory lookup indices from preloaded mappings.

    Discord mappings are inserted first so they have natural precedence
    over Telegram mappings for the same key.

    Returns (exact_name_index, clean_name_index, twitter_index).
    """
    exact_name_index = {}   # name.lower() -> mapping
    clean_name_index = {}   # _clean_name(name) -> mapping
    twitter_index = {}      # handle.lower() -> mapping

    # Discord first (wins ties)
    for m in discord_mappings:
        if m.linked_name:
            key = m.linked_name.lower()
            exact_name_index.setdefault(key, m)
            clean_key = _clean_name(m.linked_name)
            clean_name_index.setdefault(clean_key, m)
        if m.linked_twitter_handle:
            twitter_index.setdefault(m.linked_twitter_handle.lower(), m)

    # Then Telegram (fills gaps only)
    for m in telegram_mappings:
        if m.linked_name:
            key = m.linked_name.lower()
            exact_name_index.setdefault(key, m)
            clean_key = _clean_name(m.linked_name)
            clean_name_index.setdefault(clean_key, m)
        if m.linked_twitter_handle:
            twitter_index.setdefault(m.linked_twitter_handle.lower(), m)

    return exact_name_index, clean_name_index, twitter_index


def _enrich_name(name, twitter_handle, exact_index, clean_index, twitter_index):
    """Return (display_name, twitter_handle) using preloaded mapping indices.

    Identity precedence:
    1. Twitter handle exact match (highest confidence)
    2. Exact name match
    3. Fuzzy/clean name match (includes substring matching)
    """
    mapping = None

    # 1. Twitter handle match
    if twitter_handle:
        mapping = twitter_index.get(twitter_handle.lower())

    # 2. Exact name match
    if not mapping and name:
        mapping = exact_index.get(name.lower())

    # 3. Fuzzy clean name match (exact clean match + substring)
    if not mapping and name:
        name_clean = _clean_name(name)
        mapping = clean_index.get(name_clean)
        if not mapping:
            for clean_key, m in clean_index.items():
                if name_clean in clean_key or clean_key in name_clean:
                    mapping = m
                    break

    if mapping:
        return (mapping.linked_name or name, mapping.linked_twitter_handle or twitter_handle)
    return (name, twitter_handle)


def _rank_to_points(rank):
    """Convert a rank (1-based) to points: rank 1=10, rank 2=9, ..., rank 10=1, rank 11+=0."""
    if rank < 1 or rank > 10:
        return 0
    return 11 - rank


def _compute_leaderboards(exact_index, clean_index, twitter_index):
    """Compute champion and consistency leaderboards from all published rankings.

    Returns (champions_list, consistency_list, total_published_weeks).
    Each list entry is a dict with keys: rank, display_name, twitter_handle,
    champion_score, total_points, avg_rank, weeks_participated, participation_pct.

    Name merging uses two passes:
    1. Group by _clean_name (catches Alice Rozengarden / alice_rozengarden, Devin variants)
    2. Merge groups that share a twitter handle (catches Vikt0r / 0x_Vikt0r)
    """
    all_rankings = list(
        RollCallRanking.objects.filter(
            weekly_roll_call__is_published=True
        ).select_related('weekly_roll_call')
    )

    total_published_weeks = WeeklyRollCall.objects.filter(is_published=True).count()

    if not all_rankings or total_published_weeks == 0:
        return [], [], 0

    # --- Pass 1: group by _clean_name ---
    groups = defaultdict(lambda: {
        'name_variants': [],
        'twitter_handles': set(),
        'ranks': [],
        'weeks': set(),
        'total_points': 0,
    })

    for r in all_rankings:
        key = _clean_name(r.name)
        g = groups[key]
        g['name_variants'].append(r.name)
        handle = (r.twitter_handle or '').strip().strip('@').lower()
        if handle:
            g['twitter_handles'].add(handle)
        g['ranks'].append(r.rank)
        g['weeks'].add(r.weekly_roll_call.week_start_date)
        g['total_points'] += _rank_to_points(r.rank)

    # --- Pass 2: merge groups sharing a twitter handle (union-find) ---
    # Build handle -> list of clean_name keys
    handle_to_keys = defaultdict(set)
    for key, g in groups.items():
        for h in g['twitter_handles']:
            handle_to_keys[h].add(key)

    # Find groups to merge (sets of clean_name keys that share a handle)
    merged = set()
    final_groups = {}
    for key in list(groups.keys()):
        if key in merged:
            continue
        # Find all keys reachable via shared twitter handles
        cluster = {key}
        queue = [key]
        while queue:
            current = queue.pop()
            for h in groups[current]['twitter_handles']:
                for sibling in handle_to_keys[h]:
                    if sibling not in cluster:
                        cluster.add(sibling)
                        queue.append(sibling)
        # Merge all cluster members into one group
        combined = {
            'name_variants': [],
            'twitter_handles': set(),
            'ranks': [],
            'weeks': set(),
            'total_points': 0,
        }
        for k in cluster:
            g = groups[k]
            combined['name_variants'].extend(g['name_variants'])
            combined['twitter_handles'].update(g['twitter_handles'])
            combined['ranks'].extend(g['ranks'])
            combined['weeks'].update(g['weeks'])
            combined['total_points'] += g['total_points']
            merged.add(k)
        # Use the first key alphabetically as the canonical key
        canonical = min(cluster)
        final_groups[canonical] = combined

    # --- Build leaderboard entries ---
    entries = []
    for canonical_key, g in final_groups.items():
        # Most common variant as raw display name
        variant_counts = Counter(g['name_variants'])
        raw_name = variant_counts.most_common(1)[0][0]

        # Pick best twitter handle (first non-empty from the set)
        raw_twitter = ''
        for h in sorted(g['twitter_handles']):
            if h:
                raw_twitter = h
                break

        # Enrich via mapping indices
        display_name, twitter_handle = _enrich_name(
            raw_name, raw_twitter,
            exact_index, clean_index, twitter_index
        )

        weeks_participated = len(g['weeks'])
        avg_rank = sum(g['ranks']) / len(g['ranks'])
        champion_score = g['total_points'] * math.log2(1 + weeks_participated) / math.sqrt(weeks_participated)

        entries.append({
            'display_name': display_name,
            'twitter_handle': twitter_handle,
            'champion_score': champion_score,
            'total_points': g['total_points'],
            'avg_rank': avg_rank,
            'weeks_participated': weeks_participated,
            'participation_pct': (weeks_participated / total_published_weeks * 100) if total_published_weeks else 0,
        })

    # Champion leaderboard: champion_score desc, avg_rank asc tiebreak
    champions = sorted(entries, key=lambda e: (-e['champion_score'], e['avg_rank']))
    for i, entry in enumerate(champions, 1):
        entry['rank'] = i

    # Consistency leaderboard: weeks_participated desc, avg_rank asc tiebreak
    # Copy dicts so rank assignment doesn't clobber champion ranks
    consistency = [dict(e) for e in sorted(entries, key=lambda e: (-e['weeks_participated'], e['avg_rank']))]
    for i, entry in enumerate(consistency, 1):
        entry['rank'] = i

    return champions, consistency, total_published_weeks


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

    # --- Preload identity mappings (shared by weekly enrichment + leaderboards) ---
    active_discord_mappings = list(DiscordUserMapping.objects.filter(is_active=True))
    active_telegram_mappings = list(TelegramUserMapping.objects.filter(is_active=True))
    exact_index, clean_index, twitter_index = _build_mapping_indices(
        active_discord_mappings, active_telegram_mappings
    )

    # Fetch rankings for the determined week (only if a roll_call object was found and it's published)
    rankings = []
    if weekly_roll_call and weekly_roll_call.is_published:
        rankings_queryset = RollCallRanking.objects.filter(weekly_roll_call=weekly_roll_call).order_by('rank')
        rankings = list(rankings_queryset)
        for ranking in rankings:
            display_name, twitter_handle = _enrich_name(
                ranking.name, ranking.twitter_handle,
                exact_index, clean_index, twitter_index
            )
            ranking.display_name = display_name
            ranking.twitter_handle = twitter_handle

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

    # --- Compute all-time leaderboards ---
    champions, consistency, total_published_weeks = _compute_leaderboards(
        exact_index, clean_index, twitter_index
    )

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
        'champions': champions,
        'consistency': consistency,
        'total_published_weeks': total_published_weeks,
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


# =============================================================================
# Self-hosted Roll Call post pages (replaces Substack as the post is migrated)
# =============================================================================

def roll_call_index(request):
    """Reverse-chronological list of published roll call posts."""
    roll_calls = WeeklyRollCall.objects.filter(is_published=True).order_by('-week_end_date')
    context = {
        'roll_calls': roll_calls,
    }
    return render(request, 'rollcall/roll_call_index.html', context)


def roll_call_detail(request, week_end_date):
    """Render a single week's roll call post (full_text) as HTML.

    Published weeks are visible to everyone. Unpublished weeks are only
    visible as a staff preview (so the operator can sanity-check a post
    before flipping is_published) -- everyone else gets a clean 404.
    """
    from datetime import date

    try:
        parsed_date = date.fromisoformat(week_end_date)
    except ValueError:
        # URL shape matched \d{4}-\d{2}-\d{2} but isn't a real calendar date
        # (e.g. 2026-13-40) -- 404 cleanly rather than raising in the ORM.
        raise Http404("Roll call not found")

    roll_call = get_object_or_404(WeeklyRollCall, week_end_date=parsed_date)

    if not roll_call.is_published and not request.user.is_staff:
        raise Http404("Roll call not found")

    content_html = render_roll_call_markdown(roll_call.full_text)

    context = {
        'roll_call': roll_call,
        'content_html': content_html,
        'is_preview': not roll_call.is_published,
    }
    return render(request, 'rollcall/roll_call_detail.html', context)
