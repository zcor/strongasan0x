"""
Image extraction service - uses Claude vision to extract fitness data from images
"""
import base64
import logging
from typing import Optional, List, Tuple
from django.conf import settings

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """Extract the fitness/health data from this image and format it as a text attestation suitable for a weekly fitness contest. Include all metrics, numbers, and relevant details you can see. Be concise but comprehensive. Output just the attestation text, no preamble or markdown formatting."""


def extract_attestation_from_image(
    image_data: bytes,
    media_type: str = "image/jpeg",
    prompt: str = EXTRACTION_PROMPT
) -> Optional[Tuple[str, dict]]:
    """
    Extract attestation text from an image using Claude vision.

    Args:
        image_data: Raw image bytes
        media_type: MIME type (image/jpeg, image/png, image/webp, image/gif)
        prompt: Extraction prompt

    Returns:
        Tuple of (extracted_text, usage_info) or None on error
    """
    try:
        import anthropic

        if not settings.ANTHROPIC_API_KEY:
            logger.error("Anthropic API key not configured")
            return None

        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

        # Encode image to base64
        image_b64 = base64.standard_b64encode(image_data).decode('utf-8')

        message = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=1024,
            messages=[
                {
                    'role': 'user',
                    'content': [
                        {
                            'type': 'image',
                            'source': {
                                'type': 'base64',
                                'media_type': media_type,
                                'data': image_b64,
                            },
                        },
                        {
                            'type': 'text',
                            'text': prompt
                        }
                    ],
                }
            ],
        )

        extracted_text = message.content[0].text
        usage_info = {
            'input_tokens': message.usage.input_tokens,
            'output_tokens': message.usage.output_tokens,
            'model': 'claude-sonnet-4-20250514'
        }

        logger.info(f"Extracted attestation from image: {len(extracted_text)} chars, {usage_info['input_tokens']} input tokens")

        return extracted_text, usage_info

    except Exception as e:
        logger.error(f"Error extracting attestation from image: {e}")
        return None


def extract_attestation_from_multiple_images(
    images: List[Tuple[bytes, str]],
    prompt: str = EXTRACTION_PROMPT
) -> Optional[Tuple[str, dict]]:
    """
    Extract attestation text from multiple images using Claude vision.

    Args:
        images: List of (image_data, media_type) tuples
        prompt: Extraction prompt

    Returns:
        Tuple of (extracted_text, usage_info) or None on error
    """
    try:
        import anthropic

        if not settings.ANTHROPIC_API_KEY:
            logger.error("Anthropic API key not configured")
            return None

        if not images:
            logger.warning("No images provided for extraction")
            return None

        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

        # Build content with all images
        content = []
        for image_data, media_type in images:
            image_b64 = base64.standard_b64encode(image_data).decode('utf-8')
            content.append({
                'type': 'image',
                'source': {
                    'type': 'base64',
                    'media_type': media_type,
                    'data': image_b64,
                },
            })

        # Add the prompt at the end
        content.append({
            'type': 'text',
            'text': prompt
        })

        message = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=1024,
            messages=[
                {
                    'role': 'user',
                    'content': content,
                }
            ],
        )

        extracted_text = message.content[0].text
        usage_info = {
            'input_tokens': message.usage.input_tokens,
            'output_tokens': message.usage.output_tokens,
            'model': 'claude-sonnet-4-20250514',
            'image_count': len(images)
        }

        logger.info(f"Extracted attestation from {len(images)} images: {len(extracted_text)} chars")

        return extracted_text, usage_info

    except Exception as e:
        logger.error(f"Error extracting attestation from images: {e}")
        return None


async def fetch_telegram_image(bot, file_id: str) -> Optional[Tuple[bytes, str]]:
    """
    Fetch an image from Telegram using file_id.

    Args:
        bot: Telegram bot instance
        file_id: Telegram file ID

    Returns:
        Tuple of (image_bytes, media_type) or None on error
    """
    try:
        # Get file info from Telegram
        file = await bot.get_file(file_id)

        # Download the file
        file_bytes = await file.download_as_bytearray()

        # Determine media type from file path
        file_path = file.file_path.lower()
        if file_path.endswith('.png'):
            media_type = 'image/png'
        elif file_path.endswith('.gif'):
            media_type = 'image/gif'
        elif file_path.endswith('.webp'):
            media_type = 'image/webp'
        else:
            media_type = 'image/jpeg'

        logger.info(f"Fetched Telegram image: {len(file_bytes)} bytes, {media_type}")

        return bytes(file_bytes), media_type

    except Exception as e:
        logger.error(f"Error fetching Telegram image: {e}")
        return None


async def process_attestation_images(bot, attachment_info: dict) -> Optional[Tuple[str, dict]]:
    """
    Process images from an attestation's attachment_info and extract text.

    Args:
        bot: Telegram bot instance
        attachment_info: Dict containing 'photos' list with file_id entries

    Returns:
        Tuple of (extracted_text, usage_info) or None on error
    """
    if not attachment_info:
        return None

    photos = attachment_info.get('photos', [])
    if not photos:
        return None

    # Fetch all images
    images = []
    for photo in photos:
        file_id = photo.get('file_id')
        if file_id:
            result = await fetch_telegram_image(bot, file_id)
            if result:
                images.append(result)

    if not images:
        logger.warning("No images could be fetched from attachment_info")
        return None

    # Extract attestation from images
    return extract_attestation_from_multiple_images(images)
