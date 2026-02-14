"""
Webhook handler for Telegram bot
"""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
import json
import logging
import asyncio
from rollcall.telegram_bot.bot import RollCallTelegramBot

logger = logging.getLogger(__name__)

# Global bot instance
_bot_instance = None


def get_bot_instance():
    """Get or create bot instance"""
    global _bot_instance
    if _bot_instance is None:
        _bot_instance = RollCallTelegramBot()
    return _bot_instance


@csrf_exempt
@require_POST
def telegram_webhook(request):
    """
    Handle Telegram webhook requests
    Uses Django's async support if available, otherwise runs in thread
    """
    try:
        bot = get_bot_instance()
        update_data = json.loads(request.body)
        
        # Process update asynchronously
        from telegram import Update
        
        async def process_update():
            update = Update.de_json(update_data, bot.application.bot)
            await bot.application.process_update(update)
        
        # Try to use Django's async support
        try:
            from asgiref.sync import async_to_sync
            async_to_sync(process_update)()
        except ImportError:
            # Fallback to asyncio.run
            asyncio.run(process_update())
        
        return JsonResponse({'ok': True})
    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)

