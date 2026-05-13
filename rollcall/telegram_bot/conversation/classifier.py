"""Sonnet classifier — replaces the heuristic attestation detector.

Single async classify_message() returns a structured verdict:
  is_attestation, attestation_confidence, should_reply, reply_reason,
  intent, target_warrior, mentions_private_data

Uses Sonnet 4.6 with prompt caching (1h TTL on the static system block) and
forced tool_use to guarantee structured JSON output (no regex parsing).

Failure modes (network error, quota, parsing) return a conservative fallback:
should_reply=False, is_attestation=False — the bot silently drops the message
rather than acting on a bad classification. Phase A's dry-run flag means the
heuristic detector is the safety net while we tune.
"""
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import zoneinfo
from asgiref.sync import sync_to_async
from django.conf import settings

from rollcall.telegram_bot.conversation.prompt import (
    RECORD_CLASSIFICATION_TOOL,
    build_system_prompt,
    build_user_message,
    get_warrior_list_block,
)

logger = logging.getLogger(__name__)


@dataclass
class ClassifierInput:
    text: str
    chat_type: str
    is_mention_or_reply: bool
    sender_display: str
    sender_linked_warrior: Optional[str]
    pacific_now: datetime
    is_weekend_window: bool
    recent_history: list = field(default_factory=list)
    has_image: bool = False
    image_caption: Optional[str] = None


@dataclass
class ClassifierResult:
    is_attestation: bool
    attestation_confidence: float
    should_reply: bool
    reply_reason: str
    intent: str
    target_warrior: Optional[str]
    mentions_private_data: bool
    model: str
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "is_attestation": self.is_attestation,
            "attestation_confidence": self.attestation_confidence,
            "should_reply": self.should_reply,
            "reply_reason": self.reply_reason,
            "intent": self.intent,
            "target_warrior": self.target_warrior,
            "mentions_private_data": self.mentions_private_data,
            "model": self.model,
            "usage": self.usage,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


def _conservative_fallback(reason: str, model: str) -> ClassifierResult:
    """Return a do-nothing verdict when the classifier fails."""
    return ClassifierResult(
        is_attestation=False,
        attestation_confidence=0.0,
        should_reply=False,
        reply_reason=f"classifier-fallback: {reason}",
        intent="other",
        target_warrior=None,
        mentions_private_data=False,
        model=model,
        error=reason,
    )


def _looks_like_credit_or_quota_error(err) -> bool:
    """Anthropic credit-depleted / rate-limit / overloaded patterns."""
    s = str(err).lower()
    return any(k in s for k in (
        "credit balance is too low",
        "billing",
        "quota",
        "rate_limit",
        "overloaded",
        "429",
    ))


def _classify_via_deepseek(features: ClassifierInput, system_text: str, user_text: str) -> ClassifierResult:
    """Fallback classifier using DeepSeek when Anthropic fails (credits/quota).

    DeepSeek doesn't support Anthropic-style tool_use. We use the OpenAI-compatible
    JSON-mode response_format and parse the JSON manually. Less reliable than
    forced tool_use, so the calling code already treats this as a fallback path.
    """
    started = time.monotonic()
    api_key = getattr(settings, "DEEPSEEK_API_KEY", "")
    if not api_key:
        return _conservative_fallback("anthropic_failed_no_deepseek_key", "deepseek-chat")

    try:
        import openai  # DeepSeek uses the OpenAI SDK shape
    except ImportError:
        return _conservative_fallback("openai SDK not installed", "deepseek-chat")

    schema_hint = (
        "Respond with ONLY a single JSON object matching this schema:\n"
        "{\n"
        '  "is_attestation": boolean,\n'
        '  "attestation_confidence": number (0-1),\n'
        '  "should_reply": boolean,\n'
        '  "reply_reason": string,\n'
        '  "intent": "attestation"|"question_self"|"question_other_warrior"|"question_general"|"banter"|"admin"|"other",\n'
        '  "target_warrior": string|null,\n'
        '  "mentions_private_data": boolean\n'
        "}\nNo other text."
    )

    client = openai.OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_text + "\n\n" + schema_hint},
                {"role": "user", "content": user_text},
            ],
            max_tokens=512,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        logger.warning("DeepSeek fallback failed: %s", e)
        return _conservative_fallback(f"deepseek_error: {type(e).__name__}", "deepseek-chat")

    raw = resp.choices[0].message.content or ""
    try:
        import json as _json
        args = _json.loads(raw)
    except Exception:
        logger.warning("DeepSeek returned non-JSON: %s", raw[:200])
        return _conservative_fallback("deepseek_bad_json", "deepseek-chat")

    latency_ms = int((time.monotonic() - started) * 1000)
    try:
        return ClassifierResult(
            is_attestation=bool(args["is_attestation"]),
            attestation_confidence=float(args["attestation_confidence"]),
            should_reply=bool(args["should_reply"]),
            reply_reason=str(args.get("reply_reason", "")),
            intent=str(args.get("intent", "other")),
            target_warrior=args.get("target_warrior") or None,
            mentions_private_data=bool(args.get("mentions_private_data", False)),
            model="deepseek-chat",
            usage={"input_tokens": resp.usage.prompt_tokens, "output_tokens": resp.usage.completion_tokens},
            latency_ms=latency_ms,
        )
    except (KeyError, TypeError, ValueError) as e:
        return _conservative_fallback(f"deepseek_bad_args: {e}", "deepseek-chat")


def _classify_sync(features: ClassifierInput, model: str, cache_ttl: str) -> ClassifierResult:
    """Sync core — runs Anthropic SDK call. Wrapped in sync_to_async by the public API."""
    started = time.monotonic()

    if not getattr(settings, "ANTHROPIC_API_KEY", ""):
        return _conservative_fallback("ANTHROPIC_API_KEY not set", model)

    try:
        import anthropic
    except ImportError:
        return _conservative_fallback("anthropic SDK not installed", model)

    warrior_list = get_warrior_list_block()
    system_text = build_system_prompt(warrior_list)
    user_text = build_user_message(
        text=features.text,
        chat_type=features.chat_type,
        is_mention_or_reply=features.is_mention_or_reply,
        pacific_now_iso=features.pacific_now.isoformat(),
        is_weekend_window=features.is_weekend_window,
        sender_display=features.sender_display,
        sender_linked_warrior=features.sender_linked_warrior,
        recent_history=features.recent_history,
        has_image=features.has_image,
        image_caption=features.image_caption,
    )

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral", "ttl": cache_ttl},
                }
            ],
            tools=[RECORD_CLASSIFICATION_TOOL],
            tool_choice={"type": "tool", "name": "record_classification"},
            messages=[{"role": "user", "content": user_text}],
        )
    except anthropic.APIStatusError as e:
        logger.warning("Classifier API error %s: %s", e.status_code, e.message)
        # Anthropic credit-depleted / quota / overloaded → try DeepSeek fallback
        if _looks_like_credit_or_quota_error(e) or e.status_code in (429, 529):
            logger.info("Falling back to DeepSeek classifier")
            return _classify_via_deepseek(features, system_text, user_text)
        return _conservative_fallback(f"api_error_{e.status_code}", model)
    except anthropic.APIConnectionError as e:
        logger.warning("Classifier connection error: %s; trying DeepSeek", e)
        return _classify_via_deepseek(features, system_text, user_text)
    except Exception as e:
        logger.exception("Classifier unexpected error")
        return _conservative_fallback(f"unexpected: {type(e).__name__}", model)

    # Find the tool_use block — tool_choice forces it to be present
    tool_use = None
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_classification":
            tool_use = block
            break

    if tool_use is None:
        logger.warning("Classifier returned no tool_use block; stop_reason=%s", response.stop_reason)
        return _conservative_fallback("no_tool_use_block", model)

    args = tool_use.input
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
    }
    latency_ms = int((time.monotonic() - started) * 1000)

    try:
        return ClassifierResult(
            is_attestation=bool(args["is_attestation"]),
            attestation_confidence=float(args["attestation_confidence"]),
            should_reply=bool(args["should_reply"]),
            reply_reason=str(args.get("reply_reason", "")),
            intent=str(args.get("intent", "other")),
            target_warrior=args.get("target_warrior") or None,
            mentions_private_data=bool(args.get("mentions_private_data", False)),
            model=model,
            usage=usage,
            latency_ms=latency_ms,
        )
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("Classifier tool args malformed (%s): %s", e, args)
        return _conservative_fallback(f"bad_args: {e}", model)


async def classify_message(features: ClassifierInput) -> ClassifierResult:
    """Public API — async-safe."""
    model = getattr(settings, "CONVERSATION_CLASSIFIER_MODEL", "claude-sonnet-4-6")
    cache_ttl = getattr(settings, "CONVERSATION_CACHE_TTL", "1h")
    return await sync_to_async(_classify_sync)(features, model, cache_ttl)


def pacific_now() -> datetime:
    """Helper: current time in America/Los_Angeles."""
    return datetime.now(zoneinfo.ZoneInfo("America/Los_Angeles"))
