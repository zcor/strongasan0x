from django.core.management.base import BaseCommand
from django.conf import settings
import requests
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Set up Telegram webhook'

    def add_arguments(self, parser):
        parser.add_argument(
            '--webhook-url',
            type=str,
            required=True,
            help='Webhook URL to set',
        )

    def handle(self, *args, **options):
        if not settings.TELEGRAM_BOT_TOKEN:
            self.stdout.write(
                self.style.ERROR('TELEGRAM_BOT_TOKEN not set in environment variables')
            )
            return

        webhook_url = options['webhook_url']
        bot_token = settings.TELEGRAM_BOT_TOKEN
        
        # Set webhook via Telegram API
        api_url = f"https://api.telegram.org/bot{bot_token}/setWebhook"
        
        try:
            response = requests.post(api_url, json={'url': webhook_url})
            response.raise_for_status()
            
            result = response.json()
            if result.get('ok'):
                self.stdout.write(
                    self.style.SUCCESS(f'✅ Webhook set successfully: {webhook_url}')
                )
                self.stdout.write(f"Description: {result.get('description', 'N/A')}")
            else:
                self.stdout.write(
                    self.style.ERROR(f'❌ Failed to set webhook: {result.get("description", "Unknown error")}')
                )
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'Error setting webhook: {e}')
            )



