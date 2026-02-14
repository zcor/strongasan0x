from django.core.management.base import BaseCommand
from django.conf import settings
import logging
import signal
import sys
from rollcall.telegram_bot.bot import GarminTelegramBot

logger = logging.getLogger(__name__)

# Global bot instance for signal handling
_bot_instance = None


def signal_handler(sig, frame):
    """Handle shutdown signals gracefully"""
    logger.info("Received shutdown signal, stopping bot...")
    if _bot_instance:
        try:
            _bot_instance.application.stop()
        except:
            pass
    sys.exit(0)


class Command(BaseCommand):
    help = 'Run the Telegram bot in polling mode (recommended for local/desktop deployment)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--webhook',
            action='store_true',
            help='Use webhook instead of polling (requires public IP)',
        )
        parser.add_argument(
            '--webhook-url',
            type=str,
            help='Webhook URL (required for webhook mode)',
        )
        parser.add_argument(
            '--drop-pending',
            action='store_true',
            default=True,
            help='Drop pending updates on startup (default: True)',
        )
        parser.add_argument(
            '--keep-pending',
            action='store_true',
            help='Keep pending updates on startup',
        )

    def handle(self, *args, **options):
        global _bot_instance
        
        if not settings.TELEGRAM_BOT_TOKEN:
            self.stdout.write(
                self.style.ERROR('TELEGRAM_BOT_TOKEN not set in environment variables')
            )
            return

        self.stdout.write(
            self.style.SUCCESS('Starting Garmin Telegram Bot...')
        )
        self.stdout.write(f'Bot Token: {"✅ Set" if settings.TELEGRAM_BOT_TOKEN else "❌ Missing"}')

        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        try:
            bot = GarminTelegramBot()
            _bot_instance = bot
            
            if options.get('webhook'):
                webhook_url = options.get('webhook_url') or getattr(settings, 'TELEGRAM_WEBHOOK_URL', None)
                if not webhook_url:
                    self.stdout.write(
                        self.style.ERROR('Webhook URL required. Use --webhook-url or set TELEGRAM_WEBHOOK_URL')
                    )
                    return
                
                self.stdout.write(
                    self.style.WARNING(f'Starting bot with webhook: {webhook_url}')
                )
                self.stdout.write(
                    self.style.WARNING('Note: Polling mode is recommended for local deployment')
                )
                bot.start_webhook(webhook_url)
            else:
                # Default to polling mode
                drop_pending = options.get('drop_pending', True) and not options.get('keep_pending', False)
                
                self.stdout.write(
                    self.style.SUCCESS('Starting bot in polling mode (recommended for local deployment)...')
                )
                if drop_pending:
                    self.stdout.write('Dropping pending updates on startup')
                else:
                    self.stdout.write('Keeping pending updates on startup')
                
                bot.start_polling(drop_pending_updates=drop_pending)
        except KeyboardInterrupt:
            self.stdout.write(
                self.style.WARNING('\nTelegram bot stopped by user')
            )
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'Bot error: {e}')
            )
            logger.error(f'Telegram bot error: {e}', exc_info=True)
            raise

