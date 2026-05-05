"""
Command handlers for Telegram bot
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes
from asgiref.sync import sync_to_async
from rollcall.bot_commands import status, view, attest, delete_part
from rollcall.models import Attestation
from rollcall.telegram_bot.conversation.outbound import (
    send_and_log,
    KIND_COMMAND_REPLY,
    KIND_ERROR,
)

logger = logging.getLogger(__name__)


async def _reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                 *, parse_mode=None, kind: str = KIND_COMMAND_REPLY):
    """Shim: reply to a /command and log the bot's reply to MessageLog.

    Replaces the prior pattern `await update.message.reply_text(...)` so
    every command-driven outbound message is captured in the conversation
    history that the Phase A classifier and Phase B responder consume.
    """
    return await send_and_log(
        context.bot,
        update.message.chat_id,
        text,
        reply_to_message_id=update.message.message_id,
        parse_mode=parse_mode,
        kind=kind,
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command - also handles login deep links"""
    # Check if this is a login deep link
    if context.args and len(context.args) > 0:
        arg = context.args[0]
        if arg.startswith('login_'):
            await handle_login_deeplink(update, context, arg[6:])  # Strip 'login_' prefix
            return

    await _reply(
        update, context,
        "Welcome to the Roll Call Bot! 🏋️\n\n"
        "Available commands:\n"
        "/status - View attestation status for all users\n"
        "/view <username> - View full attestation for a user\n"
        "/attest - Explicitly mark your message as an attestation\n"
        "/delete_part <part_number> - Delete a part from your attestation\n"
        "/help - Show this help message"
    )


async def handle_login_deeplink(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str):
    """Handle login deep link from web dashboard"""
    from rollcall.models import WebLoginToken

    # Get the bot instance to access get_or_create_user_mapping
    bot_instance = context.bot_data.get('bot_instance')
    if not bot_instance:
        await _reply(update, context, "Bot instance not available. Please try again.", kind=KIND_ERROR)
        return

    # Get or create user mapping for this Telegram user
    user_mapping, _ = await bot_instance.get_or_create_user_mapping(update.effective_user)

    # Find and validate the token
    @sync_to_async
    def get_and_confirm_token():
        try:
            login_token = WebLoginToken.objects.get(token=token)
            if not login_token.is_valid():
                return None, "expired"
            if login_token.telegram_user:
                return None, "already_used"
            # Confirm the token
            login_token.confirm(user_mapping)
            return login_token, "success"
        except WebLoginToken.DoesNotExist:
            return None, "not_found"

    login_token, status = await get_and_confirm_token()

    if status == "not_found":
        await _reply(update, context,
            "❌ Invalid login code.\n\n"
            "Please go back to the website and get a new login code.")
    elif status == "expired":
        await _reply(update, context,
            "❌ This login code has expired.\n\n"
            "Please go back to the website and get a new login code.")
    elif status == "already_used":
        await _reply(update, context,
            "❌ This login code has already been used.\n\n"
            "If you need to log in again, please get a new code from the website.")
    else:
        # Success!
        await _reply(update, context,
            f"✅ Login confirmed!\n\n"
            f"Welcome, {update.effective_user.first_name}!\n\n"
            f"You can now go back to the website and click 'Continue' to access your dashboard.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    await start_command(update, context)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command"""
    try:
        status_message = await status.get_status_message()
        await _reply(update, context, status_message, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error in status command: {e}")
        await _reply(update, context, "Error retrieving status. Please try again later.", kind=KIND_ERROR)


async def view_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /view command"""
    if not context.args:
        await _reply(update, context, "Usage: /view <username>")
        return

    username = ' '.join(context.args)

    try:
        view_message = await view.get_view_message(username, source='telegram')
        await _reply(update, context, view_message, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error in view command: {e}")
        await _reply(update, context, "Error retrieving attestation. Please try again later.", kind=KIND_ERROR)


async def attest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /attest command - explicitly mark message as attestation"""
    # Get the bot instance to access get_or_create_user_mapping
    bot_instance = context.bot_data.get('bot_instance')
    if not bot_instance:
        await _reply(update, context, "Bot instance not available", kind=KIND_ERROR)
        return

    # Get user mapping
    user_mapping, _ = await bot_instance.get_or_create_user_mapping(update.effective_user)

    # Get message text
    text = update.message.text or update.message.caption or ""
    # Remove the command part
    if text.startswith('/attest'):
        text = text.replace('/attest', '', 1).strip()

    # If no text and message is a reply, use replied message
    if not text and update.message.reply_to_message:
        replied = update.message.reply_to_message
        text = replied.text or replied.caption or ""

    if not text:
        await _reply(update, context,
            "Please provide your attestation text, or reply to a message with /attest")
        return

    # Store attestation
    success, message, attestation_obj = await attest.store_attestation(
        source='telegram',
        user_mapping=user_mapping,
        message_id=update.message.message_id,
        chat_id=update.message.chat_id,
        text=text,
        posted_at=update.message.date,
        has_attachments=bool(update.message.photo or update.message.document),
        attachment_count=len(update.message.photo or []) + (1 if update.message.document else 0)
    )

    if success:
        # Get status to show after storing
        status_message = await status.get_status_message()
        response = f"✅ {message}\n\n{status_message}"
        await _reply(update, context, response, parse_mode='Markdown')
    else:
        await _reply(update, context, f"❌ {message}", kind=KIND_ERROR)


async def delete_part_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /delete_part command"""
    if not context.args:
        await _reply(update, context, "Usage: /delete_part <part_number>")
        return

    try:
        part_number = int(context.args[0])
    except ValueError:
        await _reply(update, context, "Part number must be an integer")
        return

    # Get user mapping
    bot_instance = context.bot_data.get('bot_instance')
    if not bot_instance:
        await _reply(update, context, "Bot instance not available", kind=KIND_ERROR)
        return
    
    user_mapping, _ = await bot_instance.get_or_create_user_mapping(update.effective_user)
    
    # Find user's attestation for current week
    def get_user_attestation():
        from rollcall.models import WeeklyRollCall
        from datetime import datetime, timezone, timedelta
        
        today = datetime.now(timezone.utc).date()
        roll_call = WeeklyRollCall.objects.filter(
            week_end_date__gte=today - timedelta(days=7)
        ).order_by('-week_start_date').first()
        
        if not roll_call:
            return None
        
        return Attestation.objects.filter(
            weekly_roll_call=roll_call,
            telegram_user=user_mapping,
            parent_attestation__isnull=True
        ).order_by('-posted_at').first()
    
    attestation = await sync_to_async(get_user_attestation)()
    
    if not attestation:
        await _reply(update, context, "No attestation found for current week")
        return

    # Check if user is admin (simplified - you may want to add proper admin check)
    is_admin = False  # TODO: Implement admin check

    success, message = await delete_part.delete_attestation_part(
        attestation.id,
        part_number,
        user_mapping=user_mapping,
        is_admin=is_admin
    )

    if success:
        await _reply(update, context, f"✅ {message}")
    else:
        await _reply(update, context, f"❌ {message}", kind=KIND_ERROR)

