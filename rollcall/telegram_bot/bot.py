"""
Telegram bot for Roll Call
Uses polling mode by default (recommended for local/desktop deployment)
Webhook mode available for servers with public IP addresses
"""
import asyncio
import logging
from collections import defaultdict
from django.conf import settings
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from rollcall.telegram_bot.handlers import commands, messages
from rollcall.models import TelegramUserMapping

logger = logging.getLogger(__name__)


class RollCallTelegramBot:
    """Telegram bot for managing attestations and rankings"""
    
    def __init__(self):
        self.token = getattr(settings, 'TELEGRAM_BOT_TOKEN', None)
        if not self.token:
            raise ValueError("TELEGRAM_BOT_TOKEN not set in settings")
        
        self.application = (
            Application.builder()
            .token(self.token)
            .concurrent_updates(True)
            .post_init(self._post_init)
            .connect_timeout(15.0)
            .read_timeout(30.0)
            .write_timeout(30.0)
            .pool_timeout(10.0)
            .get_updates_connect_timeout(15.0)
            .get_updates_read_timeout(40.0)
            .get_updates_write_timeout(30.0)
            .get_updates_pool_timeout(10.0)
            .build()
        )
        self._setup_handlers()

    async def _post_init(self, application):
        """Cache bot username at startup so handlers can detect direct mentions."""
        try:
            me = await application.bot.get_me()
            application.bot_data['bot_username'] = me.username or ''
            logger.info("Bot username cached: @%s", me.username)
        except Exception:
            logger.exception("Failed to fetch bot username at startup")
            application.bot_data['bot_username'] = ''
    
    def _setup_handlers(self):
        """Set up command and message handlers"""
        # Store bot instance in application for handlers to access
        self.application.bot_data['bot_instance'] = self

        # Per-(chat_id, telegram_user_id) lock for the multi-part attestation
        # write path. Critical now that concurrent_updates(True) is on:
        # without serialization, two near-simultaneous attestation messages
        # from the same warrior can both compute "I'm part 1" before either
        # writes, producing a torn part counter. The lock is taken inside
        # handle_message() around the find_recent_attestation + store_attestation
        # block. Per-pair keying preserves cross-warrior throughput.
        self.application.bot_data['attest_locks'] = defaultdict(asyncio.Lock)

        # Per-chat reply lock for the Phase B+ responder. Reserved here in
        # Phase 0 so the structure is in place; not yet consumed.
        self.application.bot_data['reply_locks'] = defaultdict(asyncio.Lock)
        
        # Debug: Log all updates to help troubleshoot
        async def log_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Log all updates for debugging"""
            if update.message:
                chat_type = update.message.chat.type if update.message.chat else "unknown"
                chat_id = update.message.chat_id
                user_name = update.message.from_user.username or update.message.from_user.first_name if update.message.from_user else "Unknown"
                logger.info(f"📥 Update received: {chat_type} chat {chat_id} from {user_name}")
            elif update.channel_post:
                chat_id = update.channel_post.chat_id
                logger.info(f"📥 Channel post received: chat {chat_id}")
        
        # Add debug handler first (low priority)
        self.application.add_handler(
            MessageHandler(filters.ALL, log_all_updates),
            group=-1  # Run first, before other handlers
        )
        
        # Command handlers
        self.application.add_handler(CommandHandler("start", commands.start_command))
        self.application.add_handler(CommandHandler("help", commands.help_command))
        self.application.add_handler(CommandHandler("status", commands.status_command))
        self.application.add_handler(CommandHandler("view", commands.view_command))
        self.application.add_handler(CommandHandler("attest", commands.attest_command))
        self.application.add_handler(CommandHandler("delete_part", commands.delete_part_command))
        
        # Message handler (for weekend detection and multi-part handling)
        # Handle all non-command messages (text, photos with captions, etc.)
        # We check for message.text or message.caption in the handler, so this catches everything
        self.application.add_handler(
            MessageHandler(
                ~filters.COMMAND,
                messages.handle_message
            )
        )
        
        # Error handler
        self.application.add_error_handler(self._error_handler)
    
    async def _error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle errors"""
        from telegram.error import NetworkError, TimedOut, RetryAfter, Conflict
        from rollcall.telegram_bot.conversation.outbound import send_and_log, KIND_ERROR
        err = context.error
        if isinstance(err, (NetworkError, TimedOut, RetryAfter)):
            logger.warning(f"Transient network error (will retry): {type(err).__name__}: {err}")
            return
        if isinstance(err, Conflict):
            # 409 Conflict happens when our previous getUpdates long-poll hasn't
            # timed out on Telegram's side yet (e.g. after a launchd revival).
            # Swallow it — the next poll cycle will succeed once the stale
            # server-side connection ages out (~50s).
            logger.warning(f"Conflict (stale long-poll, will retry): {err}")
            return
        logger.error(f"Exception while handling an update: {err}", exc_info=err)
        if update and update.message:
            try:
                await send_and_log(
                    context.bot,
                    update.message.chat_id,
                    "Sorry, an error occurred. Please try again later.",
                    reply_to_message_id=update.message.message_id,
                    kind=KIND_ERROR,
                )
            except Exception as e:
                logger.error(f"Error sending error message: {e}")
    
    async def get_or_create_user_mapping(self, telegram_user):
        """Get or create TelegramUserMapping for a Telegram user"""
        from asgiref.sync import sync_to_async
        from django.db import close_old_connections

        def get_or_create():
            # Close stale database connections before DB operations
            # This is required for long-running processes like bots
            close_old_connections()
            mapping, created = TelegramUserMapping.objects.get_or_create(
                telegram_user_id=telegram_user.id,
                defaults={
                    'telegram_username': telegram_user.username or '',
                    'telegram_first_name': telegram_user.first_name,
                    'telegram_last_name': telegram_user.last_name or '',
                    'is_active': True
                }
            )
            
            # Update if exists
            if not created:
                mapping.telegram_username = telegram_user.username or ''
                mapping.telegram_first_name = telegram_user.first_name
                mapping.telegram_last_name = telegram_user.last_name or ''
                mapping.save()
            
            return mapping, created
        
        return await sync_to_async(get_or_create)()
    
    def start_polling(self, drop_pending_updates=True):
        """
        Start bot with polling (recommended for local/desktop deployment)
        
        Args:
            drop_pending_updates: If True, drop pending updates on startup
        """
        logger.info("Starting Telegram bot in polling mode...")
        self.application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=drop_pending_updates,
            poll_interval=1.0,
            timeout=30,
            bootstrap_retries=-1,
        )
    
    def start_webhook(self, webhook_url, webhook_secret=None):
        """
        Start bot with webhook (for servers with public IP)
        
        Note: This is kept for compatibility but polling is recommended for local deployment
        """
        logger.info(f"Starting bot with webhook: {webhook_url}")
        self.application.run_webhook(
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
            port=getattr(settings, 'TELEGRAM_WEBHOOK_PORT', 8443)
        )

