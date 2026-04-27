"""
Metric extraction service — uses AI to extract structured metrics from attestation text.

Supports DeepSeek (default, cheap) and Anthropic (fallback).
Follows patterns from image_extraction.py and ai_ranking.py.
"""
import json
import logging
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

METRIC_FIELDS = [
    'daily_steps', 'calories_burned', 'resting_heart_rate', 'vo2_max',
    'sleep_hours', 'body_weight', 'body_fat_pct',
    'strength_sessions', 'cardio_sessions', 'combat_sessions', 'total_training_sessions',
    'protein_grams', 'calories_consumed',
    'bench_press', 'squat', 'deadlift',
]

EXTRACTION_PROMPT = """Extract structured fitness metrics from this weekly attestation text. Return a JSON object with these fields (use null for anything not mentioned or not clearly reported as a number):

- daily_steps: integer, average daily steps for the week (or total if only one number given)
- calories_burned: integer, average daily calories burned (active/exercise calories, not BMR)
- resting_heart_rate: integer, resting heart rate in bpm
- vo2_max: float, VO2 max value
- sleep_hours: float, average nightly sleep in hours
- body_weight: float, body weight in lbs (convert from kg if needed: kg * 2.205)
- body_fat_pct: float, body fat percentage
- strength_sessions: integer, number of strength/weight training sessions this week
- cardio_sessions: integer, number of cardio sessions (running, cycling, swimming, rucking, etc.)
- combat_sessions: integer, number of martial arts/boxing/wrestling/combat training sessions
- total_training_sessions: integer, total workout sessions this week (all types combined)
- protein_grams: integer, average daily protein intake in grams
- calories_consumed: integer, average daily caloric intake
- bench_press: float, bench press working weight or max in lbs (convert from kg if needed)
- squat: float, squat working weight or max in lbs (convert from kg if needed)
- deadlift: float, deadlift working weight or max in lbs (convert from kg if needed)
- extra_metrics: object, any other notable metrics not covered above (e.g. {"overhead_press": 135, "mile_time": "6:30"})

Rules:
- If a range is given (e.g. "7-8 hours sleep"), use the average (7.5)
- Weights should be in lbs. If kg is specified, convert (kg * 2.205)
- For lifts, use the heaviest working weight mentioned (not warm-up sets)
- Only extract what is explicitly stated — do not infer or estimate
- Return ONLY the JSON object, no markdown formatting or explanation"""


def get_full_text(attestation):
    """Assemble full attestation text including all child parts."""
    from rollcall.models import Attestation

    parts = Attestation.objects.filter(
        Q(id=attestation.id) | Q(parent_attestation=attestation)
    ).order_by('part_number')
    return "\n\n".join(p.raw_text for p in parts)


def _parse_response_text(response_text):
    """Parse JSON from response text, stripping markdown fences if present."""
    cleaned = response_text.strip()
    if cleaned.startswith('```'):
        first_newline = cleaned.index('\n')
        cleaned = cleaned[first_newline + 1:]
        if cleaned.endswith('```'):
            cleaned = cleaned[:-3].strip()
    return json.loads(cleaned)


def call_deepseek(text):
    """
    Call DeepSeek API to extract metrics. Uses OpenAI-compatible API.
    ~100x cheaper than Anthropic for this task.

    Returns (parsed_dict, raw_response_dict, model_name) on success.
    Raises on API or parsing failure.
    """
    import openai

    if not settings.DEEPSEEK_API_KEY:
        raise RuntimeError("DeepSeek API key not configured")

    client = openai.OpenAI(
        api_key=settings.DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com/v1",
    )

    prompt = f"{EXTRACTION_PROMPT}\n\nAttestation text:\n{text}"

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": "You are a fitness data extraction assistant. Return only valid JSON."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=1024,
    )

    response_text = response.choices[0].message.content
    model_name = response.model

    raw_response = {
        'text': response_text,
        'model': model_name,
    }
    if hasattr(response, 'usage') and response.usage:
        raw_response['input_tokens'] = response.usage.prompt_tokens
        raw_response['output_tokens'] = response.usage.completion_tokens

    parsed = _parse_response_text(response_text)
    return parsed, raw_response, model_name


def call_anthropic(text):
    """
    Call Anthropic Claude API to extract metrics (higher quality, higher cost).

    Returns (parsed_dict, raw_response_dict, model_name) on success.
    Raises on API or parsing failure.
    """
    import anthropic

    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("Anthropic API key not configured")

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    message = client.messages.create(
        model='claude-sonnet-4-20250514',
        max_tokens=1024,
        messages=[
            {
                'role': 'user',
                'content': f"{EXTRACTION_PROMPT}\n\nAttestation text:\n{text}",
            }
        ],
    )

    response_text = message.content[0].text
    model_name = message.model

    raw_response = {
        'text': response_text,
        'input_tokens': message.usage.input_tokens,
        'output_tokens': message.usage.output_tokens,
        'model': model_name,
    }

    parsed = _parse_response_text(response_text)
    return parsed, raw_response, model_name


# Provider dispatch
PROVIDERS = {
    'deepseek': call_deepseek,
    'anthropic': call_anthropic,
}

DEFAULT_PROVIDER = 'deepseek'


def call_provider(text, provider=None):
    """Call the specified (or default) AI provider for extraction."""
    provider = provider or DEFAULT_PROVIDER
    fn = PROVIDERS.get(provider)
    if not fn:
        raise ValueError(f"Unknown provider: {provider}. Available: {list(PROVIDERS.keys())}")
    return fn(text)


def extract_and_save(attestation, provider=None):
    """
    Single code path for all metric extraction.

    1. Assemble full text (parent + child parts)
    2. Call AI provider
    3. update_or_create on ExtractedMetrics:
       - On success: populate metric fields, clear extraction_error
       - On failure: set extraction_error, leave metric fields as-is
    """
    from rollcall.models import ExtractedMetrics

    now = timezone.now()
    raw_response = None
    model_name = None

    try:
        full_text = get_full_text(attestation)
        parsed, raw_response, model_name = call_provider(full_text, provider)

        # Build field updates from parsed response
        defaults = {
            'model_used': model_name,
            'raw_response': raw_response,
            'extraction_error': '',
            'last_extraction_at': now,
            'extra_metrics': parsed.get('extra_metrics') or {},
        }

        for field in METRIC_FIELDS:
            value = parsed.get(field)
            if value is not None:
                defaults[field] = value
            else:
                defaults[field] = None

        metrics, created = ExtractedMetrics.objects.update_or_create(
            attestation=attestation,
            defaults=defaults,
        )

        logger.info(
            "Extracted metrics for attestation %s (%s): %s",
            attestation.id,
            "created" if created else "updated",
            {f: defaults[f] for f in METRIC_FIELDS if defaults.get(f) is not None},
        )

        return metrics

    except Exception as e:
        logger.exception("Metric extraction failed for attestation %s", attestation.id)

        # Preserve existing metrics on failure; record error
        error_defaults = {
            'extraction_error': str(e),
            'last_extraction_at': now,
        }

        if raw_response is not None:
            error_defaults['raw_response'] = raw_response
        if model_name is not None:
            error_defaults['model_used'] = model_name
        else:
            error_defaults.setdefault('model_used', '')

        ExtractedMetrics.objects.update_or_create(
            attestation=attestation,
            defaults=error_defaults,
        )

        raise
