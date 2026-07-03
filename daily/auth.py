"""
Daily app auth — the ONE seam that bridges to rollcall.

Two paths converge on a DailyParticipant:

1. Warrior path — the request already has a rollcall Telegram session
   (set by rollcall.warrior.auth.set_telegram_session). We look up the
   TelegramUserMapping by id and resolve (or lazily create) the
   DailyParticipant that bridges to it.

2. Token path — the request URL contains a DailyAccessToken UUID. We
   resolve the token, set a SEPARATE daily session key on the request,
   and return the participant. We never touch rollcall's
   SESSION_TELEGRAM_* keys, so warrior sessions remain independent.

The @require_daily_actor decorator unifies both paths.

Separability rule: this file is the ONLY one in daily/ that imports
from rollcall. Views, services, templates, and models.py stay clean.
"""
from functools import wraps
from typing import Optional

from django.shortcuts import render
from django.utils import timezone

from rollcall.models import TelegramUserMapping
from rollcall.warrior.auth import (
    SESSION_TELEGRAM_USER_ID,
    clear_telegram_session,
    get_telegram_user_from_session,
)

from .models import DailyAccessToken, DailyParticipant

SESSION_DAILY_PARTICIPANT_ID = "daily_participant_id"

# Long-lived cookie holding the token UUID, so a token (external) user whose
# Django session has expired is silently re-authenticated instead of being
# bounced to a login wall they can't even use. Set at token_login; read by
# require_daily_actor. This is what keeps a re-engagement link working weeks
# later when the session is long gone.
DAILY_TOKEN_COOKIE = "daily_token"
DAILY_TOKEN_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year


def _get_or_create_warrior_participant(mapping: TelegramUserMapping) -> DailyParticipant:
    display_name = (
        mapping.linked_name
        or mapping.telegram_first_name
        or mapping.telegram_username
        or f"warrior_{mapping.telegram_user_id}"
    )
    participant, created = DailyParticipant.objects.get_or_create(
        telegram_mapping=mapping,
        defaults={
            "display_name": display_name,
            "kind": DailyParticipant.KIND_WARRIOR,
        },
    )
    if not created and participant.display_name != display_name and mapping.linked_name:
        # Keep display_name in sync if the warrior gets linked to a name later.
        participant.display_name = display_name
        participant.save(update_fields=["display_name", "updated_at"])
    return participant


def get_or_create_daily_link_for_mapping(mapping: TelegramUserMapping):
    """Return (link_path, participant, has_history) for a Telegram user's Daily
    app — minting the participant + an access token if needed. Used by the bot's
    /start DM flow to hand back a personal link.

    - Reuses the participant (linked to this mapping) and any existing active
      token, so a repeat /start always returns the SAME link — never rotate a
      link we may already have shared.
    - Stamps source='telegram' + a human-readable detail on first creation, so
      even a stranger/lurker who self-onboards isn't anonymous.
    - has_history = True if the user has attestations (→ the app already has
      rich context); False = "naked" (→ the in-app onboarding runs on open).

    Pure side of the seam: returns a path the caller turns into a full URL.
    """
    from rollcall.models import Attestation

    from .models import DailyAccessToken

    participant = _get_or_create_warrior_participant(mapping)

    handle = f"@{mapping.telegram_username}" if mapping.telegram_username else f"tg {mapping.telegram_user_id}"
    if not participant.source:
        participant.source = "telegram"
        participant.source_detail = f"{handle} (tg {mapping.telegram_user_id})"
        participant.save(update_fields=["source", "source_detail", "updated_at"])

    token = participant.access_tokens.filter(revoked_at__isnull=True).first()
    if token is None:
        token = DailyAccessToken.objects.create(participant=participant)

    has_history = Attestation.objects.filter(telegram_user=mapping).exists()
    return f"/daily/c/{token.token}/", participant, has_history


def get_participant_for_warrior(request) -> Optional[DailyParticipant]:
    """Resolve a DailyParticipant from an existing rollcall Telegram session."""
    telegram_user_id = get_telegram_user_from_session(request)
    if not telegram_user_id:
        return None
    try:
        mapping = TelegramUserMapping.objects.get(telegram_user_id=telegram_user_id)
    except TelegramUserMapping.DoesNotExist:
        return None
    return _get_or_create_warrior_participant(mapping)


def get_participant_for_token_session(request) -> Optional[DailyParticipant]:
    """Resolve a DailyParticipant from a previously-set daily session key."""
    participant_id = request.session.get(SESSION_DAILY_PARTICIPANT_ID)
    if not participant_id:
        return None
    try:
        return DailyParticipant.objects.get(id=participant_id, is_active=True)
    except DailyParticipant.DoesNotExist:
        return None


def get_participant_from_token_cookie(request) -> Optional[DailyParticipant]:
    """Re-authenticate a token user from the long-lived DAILY_TOKEN_COOKIE when
    their session has expired. On success, REBUILD the session so subsequent
    requests use the fast session path again. Returns None if there's no cookie
    or it's invalid/revoked (caller then falls through to the login flow)."""
    token_uuid = request.COOKIES.get(DAILY_TOKEN_COOKIE)
    if not token_uuid:
        return None
    participant = login_with_token(request, token_uuid)  # sets the session too
    return participant


def get_current_participant(request) -> Optional[DailyParticipant]:
    """Try warrior path, then token-session, then the persistent token cookie
    (silent re-auth for an expired token session)."""
    return (
        get_participant_for_warrior(request)
        or get_participant_for_token_session(request)
        or get_participant_from_token_cookie(request)
    )


def login_with_token(request, token_uuid) -> Optional[DailyParticipant]:
    """Validate a UUID token, set the daily session, return the participant.

    Returns None if the token is unknown or revoked. Does NOT touch the
    rollcall warrior session keys.
    """
    try:
        token = DailyAccessToken.objects.select_related("participant").get(token=token_uuid)
    except DailyAccessToken.DoesNotExist:
        return None
    if token.revoked_at is not None:
        return None
    if not token.participant.is_active:
        return None

    request.session[SESSION_DAILY_PARTICIPANT_ID] = token.participant.id
    token.last_used_at = timezone.now()
    token.save(update_fields=["last_used_at"])
    return token.participant


def warrior_session_keys_set(request) -> bool:
    """True if the request carries a rollcall warrior session.

    Used by views to refuse token-path access when a warrior is logged in
    (per resolved decision in plan greedy-sprouting-puppy.md).
    """
    return bool(request.session.get(SESSION_TELEGRAM_USER_ID))


def token_belongs_to_current_warrior(request, token_uuid) -> bool:
    """True when the request carries a warrior session AND the given daily
    token belongs to that SAME warrior's participant.

    A warrior who logged into the warrior dashboard carries a lingering
    Telegram session. Since a PWA install has no incognito escape hatch, that
    warrior tapping THEIR OWN daily link must just work — not hit the
    "token conflict" refusal, which only exists to stop a logged-in warrior
    from acting as a DIFFERENT person's token participant. This lets
    token_login distinguish "same person, allow" from "different person,
    conflict."
    """
    warrior_participant = get_participant_for_warrior(request)
    if warrior_participant is None:
        return False
    try:
        token = DailyAccessToken.objects.select_related("participant").get(token=token_uuid)
    except (DailyAccessToken.DoesNotExist, ValueError, TypeError):
        return False
    return token.participant_id == warrior_participant.id


def clear_warrior_session(request):
    """Drop the rollcall warrior session keys from this request's session.

    Used to recover from a STALE/orphan warrior session (e.g. a Telegram id
    with no usable mapping) that would otherwise hard-brick the daily app by
    making get_participant_for_warrior fail while warrior_session_keys_set
    stays true. Clearing lets the token/cookie path (or the signed-out page)
    take over. Delegates to rollcall's own clearer so the key list stays in
    one place."""
    clear_telegram_session(request)


def clear_daily_session(request):
    request.session.pop(SESSION_DAILY_PARTICIPANT_ID, None)


def require_daily_actor(view_func):
    """Decorator: 403 if no participant can be resolved from the session.

    Views that need authentication for arbitrary participants (e.g. the
    check-in page) use this. Pages that themselves establish a session
    (token_login) do not.
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        participant = get_current_participant(request)
        if participant is None:
            # A STALE/orphan warrior session (a Telegram id with no usable
            # mapping) would otherwise hard-brick the daily app: get_current_
            # participant can't resolve it, yet warrior_session_keys_set stays
            # true. Clear it so it can't wedge the app, then fall through to the
            # friendly signed-out page (a fresh token tap recovers cleanly).
            if warrior_session_keys_set(request):
                clear_warrior_session(request)
            # The Daily app is a TOKEN app: its home-screen PWA icon opens
            # /daily/checkin/ with no token. A user whose session has lapsed and
            # who has no re-auth cookie yet (e.g. installed the PWA before the
            # cookie fix, or cleared storage) lands here. Do NOT send them to the
            # rollcall/Telegram warrior login — a token/external user (Amy) can't
            # use it. Show a friendly "open your personal link" page instead.
            return render(request, "daily/signed_out.html", status=401)
        request.daily_participant = participant
        return view_func(request, *args, **kwargs)

    return wrapper
