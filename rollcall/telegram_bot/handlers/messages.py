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

    # For image-only messages on weekends, try to extract text from the image
    if is_image_only and is_weekend_message(message_date):
        logger.info(f"📷 Image-only message detected from {user_name}, attempting extraction...")
        extracted_text, extraction_usage = await extract_text_from_images(context.bot, message)

        if extracted_text:
            logger.info(f"✨ Extracted {len(extracted_text)} chars from image")
            # Use extracted text for attestation, prepend any caption
            if text.strip():
                text = f"{text.strip()}\n\n[Extracted from image:]\n{extracted_text}"
            else:
                text = f"[Extracted from image:]\n{extracted_text}"
        else:
            logger.warning("Could not extract text from image, skipping")
            return

    # Skip if message is too short or empty (for attestation detection)
    # But allow through if we extracted text from an image
    if len(text.strip()) < 50 and not extracted_text:
        return

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
