from django.core.management.base import BaseCommand
from django.conf import settings
import logging
import threading
import time
from bot.discord_bot import run_bot

logger = logging.getLogger(__name__)

# Configure logging to show output
logging.basicConfig(level=logging.INFO)


class Command(BaseCommand):
    help = 'Run the Discord bot alongside Django'

    def add_arguments(self, parser):
        parser.add_argument(
            '--daemon',
            action='store_true',
            help='Run bot in daemon mode',
        )

    def handle(self, *args, **options):
        if not settings.DISCORD_BOT_TOKEN:
            self.stdout.write(
                self.style.ERROR('DISCORD_BOT_TOKEN not set in environment variables')
            )
            return

        self.stdout.write(
            self.style.SUCCESS('Starting Discord Intelligence Bot...')
        )
        self.stdout.write(f'Bot Token: {"✅ Set" if settings.DISCORD_BOT_TOKEN else "❌ Missing"}')
        self.stdout.write(f'Guild ID: {"✅ Set" if settings.DISCORD_GUILD_ID else "❌ Missing"}')

        if options['daemon']:
            # Run bot in a separate thread
            bot_thread = threading.Thread(target=run_bot, daemon=True)
            bot_thread.start()
            
            self.stdout.write(
                self.style.SUCCESS('Discord bot started in daemon mode')
            )
            
            # Keep the command running
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                self.stdout.write(
                    self.style.WARNING('Stopping Discord bot...')
                )
        else:
            # Run bot in foreground
            try:
                self.stdout.write('Starting bot in foreground mode...')
                run_bot()
            except KeyboardInterrupt:
                self.stdout.write(
                    self.style.WARNING('Discord bot stopped')
                )
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f'Bot error: {e}')
                )
