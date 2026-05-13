"""
Message handlers for Telegram bot - handles weekend detection and multi-part attestations
"""
import logging
from datetime import timezone, timedelta
from telegram import Update
from telegram.ext import ContextTypes
from asgiref.sync import sync_to_async
from rollcall.telegram_bot.utils.attestation_detector import (
    is_likely_attestation,
    is_weekend_message,
    has_attestation_structure,
    has_metrics,
    has_attestation_keywords,
    has_intent_markers,
    has_conversational_markers
)
from rollcall.bot_commands import attest, status
from rollcall.models import Attestation
from django.conf import settings
from rollcall.utils.rollcalls import get_active_roll_call

logger = logging.getLogger(__name__)

# Minimum text length to consider message as having meaningful text (vs image-only)
IMAGE_ONLY_TEXT_THRESHOLD = 50


async def _sender_is_known_warrior(telegram_user_id) -> bool:
    """Has this Telegram user submitted at least one attestation before?

    Used to gate vision-API calls for image-only messages: regulars posting
    a Strava/Garmin screenshot on any day are almost certainly attesting;
    strangers posting an image probably aren't.
    """
    if not telegram_user_id:
        return False
    try:
        from rollcall.models import TelegramUserMapping
        def _check():
            from django.db import close_old_connections
            close_old_connections()
            tm = TelegramUserMapping.objects.filter(telegram_user_id=telegram_user_id).first()
            if not tm:
                return False
            return Attestation.objects.filter(telegram_user_id=tm.id, is_hidden=False).exists()
        return await sync_to_async(_check)()
    except Exception:
        logger.exception("known-warrior check failed; defaulting to False")
        return False


async def extract_text_from_images(bot, message) -> tuple[str | None, dict | None]:
    """
    Extract attestation text from images in a message using Claude vision.

    Returns:
        Tuple of (extracted_text, usage_info) or (None, None) if no images or extraction failed
    """
    if not message.photo:
        return None, None

    try:
        from rollcall.services.image_extraction import (
            fetch_telegram_image,
            extract_attestation_from_multiple_images
        )

        # Fetch all photos (get largest version of each)
        images = []
        # message.photo is a list of PhotoSize objects for the same image at different sizes
        # Get the largest one
        largest_photo = max(message.photo, key=lambda p: p.file_size or 0)

        result = await fetch_telegram_image(bot, largest_photo.file_id)
        if result:
            images.append(result)

        if not images:
            logger.warning("Could not fetch any images from message")
            return None, None

        # Extract attestation text from images (sync call wrapped)
        from asgiref.sync import sync_to_async
        extracted = await sync_to_async(extract_attestation_from_multiple_images)(images)

        if extracted:
            extracted_text, usage_info = extracted
            logger.info(f"✨ Extracted attestation from image: {len(extracted_text)} chars")
            return extracted_text, usage_info

        return None, None

    except Exception as e:
        logger.error(f"Error extracting text from images: {e}", exc_info=True)
        return None, None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle incoming messages - log all messages and detect attestations on weekends
    Handles both regular messages (DMs, groups) and channel posts
    """
    # Handle both regular messages and channel posts
    message = update.message or update.channel_post
    if not message:
        return
    
    # Skip bot messages (but channel posts from bots are OK if they're not the bot itself)
    if message.from_user and message.from_user.is_bot:
        # Only skip if it's the bot itself
        if message.from_user.id == context.bot.id:
            return
    
    text = message.text or message.caption or ""
    
    # Log chat type for debugging
    chat_type = message.chat.type if message.chat else "unknown"
    chat_id = message.chat_id
    
    # Get user info (channel posts might not have from_user)
    if message.from_user:
        user_name = message.from_user.username or message.from_user.first_name or "Unknown"
        user_obj = message.from_user
    else:
        # Channel post without author (anonymous channel)
        user_name = "Channel Post"
        user_obj = None
    
    logger.info(f"Received message from {user_name} in {chat_type} (chat_id: {chat_id}): {text[:50]}...")
    
    # Log all messages for later review (even if they don't become attestations)
    try:
        from rollcall.bot_commands.log_message import log_message
        bot_instance = context.bot_data.get('bot_instance')
        if bot_instance and user_obj:
            user_mapping, _ = await bot_instance.get_or_create_user_mapping(user_obj)
            
            # Collect attachment information
            attachment_info = {}
            if message.photo:
                # Get the largest photo
                largest_photo = max(message.photo, key=lambda p: p.file_size or 0)
                attachment_info['photos'] = [{
                    'file_id': largest_photo.file_id,
                    'file_unique_id': largest_photo.file_unique_id,
                    'width': largest_photo.width,
                    'height': largest_photo.height,
                    'file_size': largest_photo.file_size,
                }]
            if message.document:
                attachment_info['document'] = {
                    'file_id': message.document.file_id,
                    'file_unique_id': message.document.file_unique_id,
                    'file_name': message.document.file_name,
                    'mime_type': message.document.mime_type,
                    'file_size': message.document.file_size,
                }
            if message.video:
                attachment_info['video'] = {
                    'file_id': message.video.file_id,
                    'file_unique_id': message.video.file_unique_id,
                    'width': message.video.width,
                    'height': message.video.height,
                    'duration': message.video.duration,
                    'file_size': message.video.file_size,
                }
            if message.audio:
                attachment_info['audio'] = {
                    'file_id': message.audio.file_id,
                    'file_unique_id': message.audio.file_unique_id,
                    'duration': message.audio.duration,
                    'performer': message.audio.performer,
                    'title': message.audio.title,
                    'file_size': message.audio.file_size,
                }
            if message.voice:
                attachment_info['voice'] = {
                    'file_id': message.voice.file_id,
                    'file_unique_id': message.voice.file_unique_id,
                    'duration': message.voice.duration,
                    'mime_type': message.voice.mime_type,
                    'file_size': message.voice.file_size,
                }
            
            success, log_msg, message_log = await log_message(
                source='telegram',
                user_mapping=user_mapping,
                message_id=message.message_id,
                chat_id=message.chat_id,
                content=text,
                posted_at=message.date.replace(tzinfo=timezone.utc) if message.date.tzinfo is None else message.date,
                has_attachments=bool(message.photo or message.document or message.video or message.audio or message.voice),
                attachment_count=len(message.photo or []) + (1 if message.document else 0) + (1 if message.video else 0) + (1 if message.audio else 0) + (1 if message.voice else 0),
                attachment_info=attachment_info
            )
            if success:
                logger.info(f"✅ Logged message {message.message_id} from {chat_type} chat {chat_id}")
            else:
                logger.warning(f"❌ Failed to log message: {log_msg}")
        elif not user_obj:
            logger.warning(f"Channel post without author - cannot create user mapping (chat_id: {chat_id})")
        else:
            logger.warning("Bot instance not available in context")
    except Exception as e:
        logger.error(f"Error logging Telegram message: {e}", exc_info=True)
    
    message_date = message.date

    # Check if this is an image-only message (photo with minimal text)
    is_image_only = bool(message.photo) and len(text.strip()) < IMAGE_ONLY_TEXT_THRESHOLD
    extracted_text = None
    extraction_usage = None

    # Decide whether to attempt image extraction.
    # Original rule was weekend-only (Sunday roll-call window) to limit
    # vision-API spend on random food/meme photos. Relaxed 2026-05-13: also
    # extract any day when the sender is a known warrior (prior attestation
    # history), since Strava/Garmin screenshots from regulars are reliably
    # attestation-shaped — Corn's 209-mi cycling screenshot on a Tuesday
    # would have been silently dropped under the old rule.
    should_extract = is_image_only and (
        is_weekend_message(message_date)
        or await _sender_is_known_warrior(message.from_user.id if message.from_user else None)
    )

    if should_extract:
        logger.info(f"📷 Image-only message from {user_name}, attempting extraction...")
        extracted_text, extraction_usage = await extract_text_from_images(context.bot, message)

        if extracted_text:
            logger.info(f"✨ Extracted {len(extracted_text)} chars from image")
            # Use extracted text for attestation, prepend any caption
            if text.strip():
                text = f"{text.strip()}\n\n[Extracted from image:]\n{extracted_text}"
            else:
                text = f"[Extracted from image:]\n{extracted_text}"
        else:
            # Extraction failed (vision model couldn't read it, or no fitness
            # content). Fall through to normal handling rather than dropping
            # the whole message — caption text or future classifier logic
            # may still find something useful.
            logger.info("Image extraction returned no text; continuing without it")
            extracted_text = None

    # Direct-mention / reply-to-bot detection. Computed once, used by both the
    # short-message gate below and the classifier (as an input feature).
    bot_username = (context.bot_data.get('bot_username') or '').lower()
    is_mention_or_reply = False
    if bot_username:
        if message.text and (f'@{bot_username}' in message.text.lower()):
            is_mention_or_reply = True
        elif message.caption and (f'@{bot_username}' in message.caption.lower()):
            is_mention_or_reply = True
    if message.reply_to_message and message.reply_to_message.from_user:
        try:
            if message.reply_to_message.from_user.id == context.bot.id:
                is_mention_or_reply = True
        except Exception:
            pass

    # Skip if message is too short or empty.
    # Heuristic path needs ≥50 chars to score reliably. Classifier path is
    # cheaper to run on short messages and short DMs ("you there?", "hi") are
    # legitimate things Bull should respond to. So:
    #   - DMs: classify everything ≥1 char
    #   - Direct @-mentions or replies to the bot in groups: classify (short "no offense @Bot" should reach the classifier)
    #   - Other groups: keep the 50-char floor (classifier still skips emoji-spam)
    classifier_on = getattr(settings, 'CONVERSATION_CLASSIFIER_ENABLED', False)
    is_dm = chat_type == 'private'
    if not text.strip() and not extracted_text:
        return
    if not extracted_text:
        if classifier_on and (is_dm or is_mention_or_reply):
            pass  # non-empty DM, or direct mention/reply in a group, goes to classifier
        elif len(text.strip()) < 50:
            return

    # ── Phase A: Sonnet classifier ────────────────────────────────────────
    # Replaces the heuristic detector (kept below as a fallback when the
    # classifier flag is off). When CONVERSATION_CLASSIFIER_ENABLED is True,
    # the classifier verdict drives both the attestation path and (Phase B+)
    # the conversational reply path. Verdict is persisted to MessageLog
    # regardless, for offline tuning via replay_classifier mgmt cmd.

    classifier_verdict = None
    classifier_is_attestation = None  # tri-state: None = no verdict, True/False = use it
    if getattr(settings, 'CONVERSATION_CLASSIFIER_ENABLED', False):
        from rollcall.telegram_bot.conversation.classifier import (
            ClassifierInput,
            classify_message,
            pacific_now,
        )

        # Try to resolve the sender's linked warrior name (best effort).
        sender_linked_warrior = None
        try:
            from rollcall.models import TelegramUserMapping
            from asgiref.sync import sync_to_async as _sta
            def _lookup():
                from django.db import close_old_connections
                close_old_connections()
                m = TelegramUserMapping.objects.filter(telegram_user_id=message.from_user.id).first()
                return m.linked_name if m and m.linked_name else None
            sender_linked_warrior = await _sta(_lookup)()
        except Exception:
            pass

        features = ClassifierInput(
            text=text,
            chat_type=chat_type,
            is_mention_or_reply=is_mention_or_reply,
            sender_display=user_name,
            sender_linked_warrior=sender_linked_warrior,
            pacific_now=pacific_now(),
            is_weekend_window=is_weekend_message(message_date),
            recent_history=[],  # Phase B will populate from MessageLog
            has_image=bool(message.photo),
            image_caption=message.caption,
        )

        try:
            verdict = await classify_message(features)
            classifier_verdict = verdict.to_dict()
            classifier_is_attestation = verdict.is_attestation
            logger.info(
                "Classifier: is_att=%s conf=%.2f reply=%s intent=%s latency=%dms model=%s",
                verdict.is_attestation, verdict.attestation_confidence,
                verdict.should_reply, verdict.intent, verdict.latency_ms, verdict.model,
            )

            # Persist the verdict back onto the existing MessageLog row for this message.
            try:
                from rollcall.models import MessageLog
                from asgiref.sync import sync_to_async as _sta
                def _update_verdict():
                    from django.db import close_old_connections
                    close_old_connections()
                    MessageLog.objects.filter(
                        source='telegram',
                        telegram_chat_id=message.chat_id,
                        telegram_message_id=message.message_id,
                    ).update(classifier_verdict=classifier_verdict)
                await _sta(_update_verdict)()
            except Exception:
                logger.exception("Failed to persist classifier_verdict to MessageLog")
        except Exception:
            logger.exception("Classifier raised; falling back to heuristic")
            classifier_is_attestation = None  # fall through to heuristic

    # ── Heuristic detector (used when classifier disabled OR raised) ─────
    if classifier_is_attestation is None:
        # Check for substance (structure or metrics)
        has_structure = has_attestation_structure(text)
        has_metric = has_metrics(text)
        has_substance = has_structure or has_metric

        # Calculate attestation score to check high-scoring messages even on non-weekend days
        score = 0
        if has_intent_markers(text):
            score += 2
        if has_structure:
            score += 2
        if has_metric:
            score += 2  # INCREASED from 1 to 2
        if has_attestation_keywords(text):
            score += 1

        # Conversational penalty: only apply if no substance
        if has_conversational_markers(text) and not has_substance:
            score -= 1

        # Image-extracted attestations get a bonus score
        if extracted_text:
            score += 3

        # Check if it's a weekend message OR if it has a very high score (4+)
        # High-scoring messages are likely attestations even if posted on non-weekend days
        is_weekend = is_weekend_message(message_date)
        is_high_score = score >= 4

        if not is_weekend and not is_high_score:
            return

        # Check if message looks like an attestation
        # Skip this check for image-extracted text (we already validated it came from an image)
        if not extracted_text:
            # For high-score messages, check without weekend requirement
            if is_high_score and not is_weekend:
                # Use relaxed check (no weekend requirement) for high-scoring messages
                if not is_likely_attestation(text, message_date=None, min_length=50):
                    return
            else:
                # Normal check with weekend requirement
                if not is_likely_attestation(text, message_date):
                    return
    else:
        # Classifier-driven branch.
        # ── Phase B/D: conversational reply via Claude CLI ────────────────
        # In groups: only reply on direct mention/reply (the strict guardrail) —
        # the classifier may set should_reply=True for ambient group questions
        # too, but we don't want Bull butting in. mention/reply is required.
        # In DMs: classifier verdict alone is enough.
        # CONVERSATION_REPLIES_DM_ONLY (legacy flag): if set, blocks ALL group
        # replies even on mention. Default False now that group is unlocked.
        if (
            classifier_verdict
            and classifier_verdict.get("should_reply")
            and getattr(settings, 'CONVERSATION_REPLIES_ENABLED', False)
        ):
            dm_only = getattr(settings, 'CONVERSATION_REPLIES_DM_ONLY', False)
            is_dm_chat = chat_type == 'private'
            if dm_only and not is_dm_chat:
                logger.info("Reply suppressed: DM_ONLY mode and chat is %s", chat_type)
            elif not is_dm_chat and not is_mention_or_reply:
                logger.info("Reply suppressed: group chat without mention/reply")
            else:
                from rollcall.telegram_bot.conversation.responder import (
                    ReplyContext,
                    generate_reply,
                )
                from rollcall.telegram_bot.conversation.outbound import send_and_log, KIND_REPLY

                # Resolve linked warrior name (best effort, may already be cached above)
                viewer_warrior = None
                try:
                    from rollcall.models import TelegramUserMapping
                    from asgiref.sync import sync_to_async as _sta
                    def _lookup_warrior():
                        from django.db import close_old_connections
                        close_old_connections()
                        m = TelegramUserMapping.objects.filter(telegram_user_id=message.from_user.id).first()
                        return m.linked_name if m and m.linked_name else None
                    viewer_warrior = await _sta(_lookup_warrior)()
                except Exception:
                    pass

                rctx = ReplyContext(
                    user_text=text,
                    chat_id=message.chat_id,
                    chat_type=chat_type,
                    viewer_telegram_id=message.from_user.id,
                    viewer_display=user_name,
                    viewer_warrior=viewer_warrior,
                    intent=classifier_verdict.get("intent", "other"),
                    target_warrior=classifier_verdict.get("target_warrior"),
                    recent_history=[],  # populated in Phase D / future iteration
                )

                # Per-chat reply lock: serialize replies in the same chat so a
                # follow-up message doesn't get a stale-context response.
                reply_locks = context.bot_data.get('reply_locks')
                lock = reply_locks[message.chat_id] if reply_locks is not None else None
                if lock is not None:
                    await lock.acquire()
                try:
                    # Show typing indicator immediately so warrior sees Bull is thinking
                    try:
                        await context.bot.send_chat_action(chat_id=message.chat_id, action='typing')
                    except Exception:
                        pass
                    result = await generate_reply(rctx)
                    if result.text:
                        await send_and_log(
                            context.bot, message.chat_id, result.text,
                            reply_to_message_id=message.message_id,
                            kind=KIND_REPLY,
                        )
                    else:
                        logger.warning("responder returned no text: %s", result.error)
                finally:
                    if lock is not None:
                        lock.release()

        # If it's not an attestation, we're done (replies handled above).
        if not classifier_is_attestation:
            return
        # If the classifier says yes, fall through to the multi-part + store path below.

    # Phase A safety net: dry-run flag short-circuits the store path so we
    # can shadow-test the classifier for a weekend without polluting attestations.
    if getattr(settings, 'CONVERSATION_DRY_RUN_ATTESTATIONS', False):
        logger.info("DRY RUN: classifier said attestation, but skipping store_attestation")
        return

    # Get user mapping
    bot_instance = context.bot_data.get('bot_instance')
    if not bot_instance:
        return

    user_mapping, _ = await bot_instance.get_or_create_user_mapping(message.from_user)
    
    # Check for existing attestation in time window (multi-part detection)
    window_minutes = getattr(settings, 'ATTESTATION_MULTI_PART_WINDOW_MINUTES', 15)
    time_window_start = message_date - timedelta(minutes=window_minutes)
    
    def find_recent_attestation():
        from django.db import close_old_connections
        close_old_connections()
        roll_call = get_active_roll_call(reference_dt=message_date)
        
        # If still no roll_call (e.g., if a published one existed for this week and we prevented creation)
        if not roll_call:
            return None, 1
        
        # Find recent attestation from same user within the time window for multi-part detection
        recent = Attestation.objects.filter(
            weekly_roll_call=roll_call,
            telegram_user=user_mapping,
            posted_at__gte=time_window_start,
            posted_at__lte=message.date
        ).order_by('-posted_at').first()
        
        if recent:
            # Get the parent attestation
            parent = recent.parent_attestation or recent
            # Get max part number
            from django.db.models import Q
            parts = Attestation.objects.filter(
                Q(id=parent.id) | Q(parent_attestation=parent)
            )
            max_part = max([p.part_number for p in parts]) if parts else 1
            return parent, max_part + 1
        
        return None, 1
    
    # Build attachment_info for storage (no shared state — safe outside the lock)
    attestation_attachment_info = {}
    if message.photo:
        largest_photo = max(message.photo, key=lambda p: p.file_size or 0)
        attestation_attachment_info['photos'] = [{
            'file_id': largest_photo.file_id,
            'file_unique_id': largest_photo.file_unique_id,
            'width': largest_photo.width,
            'height': largest_photo.height,
            'file_size': largest_photo.file_size,
        }]
    if message.document:
        attestation_attachment_info['document'] = {
            'file_id': message.document.file_id,
            'file_unique_id': message.document.file_unique_id,
            'file_name': message.document.file_name,
            'mime_type': message.document.mime_type,
            'file_size': message.document.file_size,
        }

    # Multi-part race fix: with concurrent_updates(True), two near-simultaneous
    # attestation messages from the same warrior in the same chat would race —
    # both compute "I'm part 1" before either writes, producing a torn part
    # counter or duplicate-part-1 rows. Take a per-(chat, user) lock so the
    # find-recent + store sequence is atomic. Per-pair keying keeps cross-warrior
    # throughput intact.
    attest_locks = context.bot_data.get('attest_locks')
    lock_key = (message.chat_id, message.from_user.id) if message.from_user else (message.chat_id, None)
    lock = attest_locks[lock_key] if attest_locks is not None else None

    if lock is not None:
        await lock.acquire()
    try:
        parent_attestation, part_number = await sync_to_async(find_recent_attestation)()

        # Store attestation
        success, message_text, attestation_obj = await attest.store_attestation(
            source='telegram',
            user_mapping=user_mapping,
            message_id=message.message_id,
            chat_id=message.chat_id,
            text=text,
            posted_at=message_date,
            has_attachments=bool(message.photo or message.document),
            attachment_count=len(message.photo or []) + (1 if message.document else 0),
            parent_attestation=parent_attestation,
            part_number=part_number,
            attachment_info=attestation_attachment_info if attestation_attachment_info else None,
            image_extraction_info=extraction_usage
        )
    finally:
        if lock is not None:
            lock.release()

    if success:
        # Politely confirm storage
        from rollcall.telegram_bot.conversation.outbound import send_and_log, KIND_ATTESTATION_ACK
        if extracted_text:
            confirm_msg = "✨ Extracted fitness data from your image and stored your attestation!"
        elif part_number > 1:
            confirm_msg = f"✅ Stored as part {part_number} of your attestation."
        else:
            confirm_msg = "✅ Stored your attestation for this week."

        # Show status
        try:
            status_message = await status.get_status_message()
            full_response = f"{confirm_msg}\n\n{status_message}"
            await send_and_log(
                context.bot, message.chat_id, full_response,
                reply_to_message_id=message.message_id, parse_mode='Markdown',
                kind=KIND_ATTESTATION_ACK,
            )
        except Exception as e:
            logger.error(f"Error getting status: {e}")
            await send_and_log(
                context.bot, message.chat_id, confirm_msg,
                reply_to_message_id=message.message_id,
                kind=KIND_ATTESTATION_ACK,
            )
    else:
        logger.warning(f"Failed to store attestation: {message_text}")
