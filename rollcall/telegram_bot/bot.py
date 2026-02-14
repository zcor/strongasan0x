"""
Telegram bot for Roll Call
Uses polling mode by default (recommended for local/desktop deployment)
Webhook mode available for servers with public IP addresses
"""
import logging
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
        
        self.application = Application.builder().token(self.token).build()
        self._setup_handlers()
    
    def _setup_handlers(self):
        """Set up command and message handlers"""
        # Store bot instance in application for handlers to access
        self.application.bot_data['bot_instance'] = self
        
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
        logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)
        if update and update.message:
            try:
                await update.message.reply_text(
                    "Sorry, an error occurred. Please try again later."
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
            drop_pending_updates=drop_pending_updates
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

