"""
AI Ranking Service Module

Provides functions to call various AI providers for ranking health attestations.
Supports OpenAI, Anthropic, Grok, and DeepSeek APIs.
"""

import json
import random
import logging
from typing import Optional, Dict, List, Tuple
from django.conf import settings

logger = logging.getLogger(__name__)

# Ranking prompt template
RANKING_PROMPT = """You are recruiting an army for a great battle. Your job is to read through health attestations they have provided and rank the participants. Do not overweight thorough accounting, volume of tracking or thorough logging, because all warriors presumably have a baseline of underreported routine daily activities in addition to explicitly noted accomplishments. Prioritize for battle-readiness and judge the quality and intensity underlying reported feats.

Specifically:
- A warrior who logs their activities with a watch (auto-generated step counts, HR zones, calorie estimates, per-minute breakdowns) is not more battle-ready than one who tersely lists harder lifts. Reward intensity and difficulty over data legibility.
- Daily step counts above ~10K are mostly non-training daily movement (commuting, errands, walking around). Do not treat 100K+ weekly steps as 100K steps of effortful training.
- Sauna sessions, walks, and stretching are recovery, not work. They count for consistency but not for intensity.
- A long list of distinct activities is not the same as a hard week. Three brutal sessions beat seven half-efforts. Look at session duration, HR, weight loaded, and whether the warrior pushed to exhaustion.
- A single-discipline week of extreme depth (e.g. a 200-mile cycling week, a multi-class combat-sports week) can rank above a broader-but-thinner week.

IMPORTANT: Some participants may have included image attachments with their attestations. When you see notes like "[NOTE: This participant has included an image attachment...]", pay special attention to the visual evidence described, as it provides concrete performance data that should be factored into your ranking.

Output just a ranked list in JSON format as an array of objects with "rank" (integer starting at 1) and "name" (string) fields.

Example format:
[
  {"rank": 1, "name": "Warrior Name"},
  {"rank": 2, "name": "Another Warrior"},
  ...
]"""

# Thinking mode prompt - asks AI to show reasoning
THINKING_PROMPT_ADDITION = """\n\nIMPORTANT: Before providing your final ranking, please show your thinking process. Consider:
1. What metrics and achievements stand out for battle-readiness?
2. Which participants demonstrated exceptional intensity or quality of effort?
3. How do you weigh different types of accomplishments (strength, endurance, consistency, etc.)?
4. What factors might indicate underreported activities vs. explicit accomplishments?

After your analysis, provide the final JSON ranking."""


def get_available_providers() -> List[str]:
    """
    Check which AI provider API keys are available.

    Returns:
        List of available provider names: ['anthropic', 'deepseek']
    """
    available = []

    # OpenAI disabled - not producing quality rankings
    # if settings.OPENAI_API_KEY:
    #     available.append('openai')

    if settings.ANTHROPIC_API_KEY:
        available.append('anthropic')

    if settings.GROK_API_KEY:
        available.append('grok')

    if settings.DEEPSEEK_API_KEY:
        available.append('deepseek')

    return available


def select_random_provider() -> Optional[str]:
    """
    Randomly select from available AI providers.
    
    Returns:
        Provider name or None if no providers are available
    """
    available = get_available_providers()
    if not available:
        return None
    return random.choice(available)


def estimate_cost_openai(input_tokens: int, output_tokens: int, model: str = "gpt-4") -> Dict[str, float]:
    """
    Estimate cost for OpenAI API call.
    
    Pricing (as of 2024):
    - gpt-4: $0.03/1K input tokens, $0.06/1K output tokens
    - gpt-4-turbo: $0.01/1K input tokens, $0.03/1K output tokens
    
    Args:
        input_tokens: Estimated input tokens
        output_tokens: Estimated output tokens
        model: Model name
    
    Returns:
        Dictionary with cost breakdown
    """
    # Rough token estimation: 1 token ≈ 4 characters
    if "turbo" in model.lower():
        input_price_per_1k = 0.01
        output_price_per_1k = 0.03
    else:
        input_price_per_1k = 0.03
        output_price_per_1k = 0.06
    
    input_cost = (input_tokens / 1000) * input_price_per_1k
    output_cost = (output_tokens / 1000) * output_price_per_1k
    total_cost = input_cost + output_cost
    
    return {
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'input_cost': input_cost,
        'output_cost': output_cost,
        'total_cost': total_cost,
        'model': model
    }


def estimate_cost_anthropic(input_tokens: int, output_tokens: int, model: str = "claude-3-opus") -> Dict[str, float]:
    """
    Estimate cost for Anthropic API call.
    
    Pricing (as of 2024):
    - claude-3-opus: $15/1M input tokens, $75/1M output tokens
    - claude-3-sonnet: $3/1M input tokens, $15/1M output tokens
    
    Args:
        input_tokens: Estimated input tokens
        output_tokens: Estimated output tokens
        model: Model name
    
    Returns:
        Dictionary with cost breakdown
    """
    if "opus" in model.lower():
        input_price_per_1m = 15.0
        output_price_per_1m = 75.0
    elif "sonnet" in model.lower():
        input_price_per_1m = 3.0
        output_price_per_1m = 15.0
    else:
        # Default to sonnet pricing
        input_price_per_1m = 3.0
        output_price_per_1m = 15.0
    
    input_cost = (input_tokens / 1_000_000) * input_price_per_1m
    output_cost = (output_tokens / 1_000_000) * output_price_per_1m
    total_cost = input_cost + output_cost
    
    return {
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'input_cost': input_cost,
        'output_cost': output_cost,
        'total_cost': total_cost,
        'model': model
    }


def estimate_cost_deepseek(input_tokens: int, output_tokens: int, model: str = "deepseek-chat") -> Dict[str, float]:
    """
    Estimate cost for DeepSeek API call.
    
    Pricing (as of 2024):
    - deepseek-chat: $0.14/1M input tokens, $0.28/1M output tokens
    - deepseek-coder: $0.14/1M input tokens, $0.28/1M output tokens
    
    Args:
        input_tokens: Estimated input tokens
        output_tokens: Estimated output tokens
        model: Model name
    
    Returns:
        Dictionary with cost breakdown
    """
    # DeepSeek pricing is very affordable
    input_price_per_1m = 0.14
    output_price_per_1m = 0.28
    
    input_cost = (input_tokens / 1_000_000) * input_price_per_1m
    output_cost = (output_tokens / 1_000_000) * output_price_per_1m
    total_cost = input_cost + output_cost
    
    return {
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'input_cost': input_cost,
        'output_cost': output_cost,
        'total_cost': total_cost,
        'model': model
    }


def estimate_tokens(text: str) -> int:
    """
    Rough estimation of token count.
    Rule of thumb: 1 token ≈ 4 characters for English text.
    
    Args:
        text: Text to estimate
    
    Returns:
        Estimated token count
    """
    return len(text) // 4


def call_openai(attestations_text: str, prompt: str = RANKING_PROMPT, thinking_mode: bool = False, use_file_attachment: bool = False) -> Optional[Tuple[str, str, Dict]]:
    """
    Call OpenAI API to generate rankings.
    
    Args:
        attestations_text: Formatted text of all attestations
        prompt: Prompt template (defaults to RANKING_PROMPT)
        thinking_mode: If True, ask AI to show reasoning process
        use_file_attachment: If True, use file attachment (Note: OpenAI chat completions doesn't support files directly, so this will still be inline)
    
    Returns:
        Tuple of (model_name, response_text, cost_info) or None on error
    """
    try:
        import openai
        
        if not settings.OPENAI_API_KEY:
            logger.error("OpenAI API key not configured")
            return None
        
        client = openai.OpenAI(api_key=settings.OPENAI_API_KEY)
        
        # Add thinking mode if requested
        final_prompt = prompt
        if thinking_mode:
            final_prompt = prompt + THINKING_PROMPT_ADDITION
        
        # Note: OpenAI chat completions API doesn't support file attachments directly
        # For file support, we'd need to use Assistants API, but for now we'll keep inline
        # If use_file_attachment is True, we'll still include inline but note it in the prompt
        if use_file_attachment:
            # Create a cleaner prompt that references the attestations
            full_prompt = f"{final_prompt}\n\nAttestations (attached as text):\n{attestations_text}\n\nOutput the JSON ranking list:"
        else:
            full_prompt = f"{final_prompt}\n\nAttestations:\n{attestations_text}\n\nOutput the JSON ranking list:"
        
        # Estimate tokens for cost calculation
        input_tokens_est = estimate_tokens(full_prompt)
        
        # Choose model based on input size and thinking mode
        # GPT-4 has 8K context window, GPT-4 Turbo has 128K
        # Use turbo if input is large or thinking mode is enabled
        if input_tokens_est > 6000 or thinking_mode:
            model = "gpt-4-turbo-preview"  # or "gpt-4-1106-preview" depending on what's available
            max_output_tokens = 4000 if thinking_mode else 2000
            # Turbo has 128K context, so we can use full max_tokens
        else:
            model = "gpt-4"
            # For regular GPT-4, ensure we don't exceed context window
            # Reserve space for output: 8192 - input_tokens_est - buffer
            available_for_output = 8192 - input_tokens_est - 100  # 100 token buffer
            max_output_tokens = min(4000 if thinking_mode else 2000, max(100, available_for_output))
            if max_output_tokens < 500:
                logger.warning(f"Input too large for gpt-4 ({input_tokens_est:,} tokens). Switching to gpt-4-turbo.")
                model = "gpt-4-turbo-preview"
                max_output_tokens = 4000 if thinking_mode else 2000
        
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are an expert at evaluating warrior fitness and battle-readiness."},
                {"role": "user", "content": full_prompt}
            ],
            temperature=0.7,
            max_tokens=max_output_tokens
        )
        
        response_text = response.choices[0].message.content
        model_name = response.model
        
        # Get actual token usage if available
        if hasattr(response, 'usage'):
            input_tokens = response.usage.prompt_tokens
            output_tokens = response.usage.completion_tokens
        else:
            input_tokens = input_tokens_est
            output_tokens = estimate_tokens(response_text)
        
        cost_info = estimate_cost_openai(input_tokens, output_tokens, model_name)
        
        return (model_name, response_text, cost_info)
    
    except ImportError:
        logger.error("openai package not installed")
        return None
    except Exception as e:
        logger.error(f"OpenAI API error: {e}")
        return None


def call_anthropic(attestations_text: str, prompt: str = RANKING_PROMPT, thinking_mode: bool = False, use_file_attachment: bool = False) -> Optional[Tuple[str, str, Dict]]:
    """
    Call Anthropic API to generate rankings.
    
    Args:
        attestations_text: Formatted text of all attestations
        prompt: Prompt template (defaults to RANKING_PROMPT)
        thinking_mode: If True, ask AI to show reasoning process
        use_file_attachment: If True, attach attestations as a text file instead of inline
    
    Returns:
        Tuple of (model_name, response_text, cost_info) or None on error
    """
    try:
        import anthropic
        
        if not settings.ANTHROPIC_API_KEY:
            logger.error("Anthropic API key not configured")
            return None
        
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        
        # Add thinking mode if requested
        final_prompt = prompt
        if thinking_mode:
            final_prompt = prompt + THINKING_PROMPT_ADDITION
        
        # Set max_tokens (claude-sonnet-4 supports up to 16384 output tokens)
        # For thinking mode, we use a higher limit to allow for reasoning + JSON
        max_output_tokens = 8192
        
        if use_file_attachment:
            # Use separate content blocks for better structure
            # This separates instructions from data, which can help the AI parse better
            # Note: For text-only content, this is mainly organizational - token count is the same
            # The real benefit would be for actual binary files (images, PDFs) which we're not doing yet
            message_content = [
                {
                    "type": "text",
                    "text": f"{final_prompt}\n\nBelow are all the attestations from participants. Please read through them and provide the JSON ranking list."
                },
                {
                    "type": "text",
                    "text": f"ATTESTATIONS:\n\n{attestations_text}"
                }
            ]
            
            # Estimate tokens (prompt + attestations)
            prompt_tokens = estimate_tokens(final_prompt)
            attestations_tokens = estimate_tokens(attestations_text)
            input_tokens_est = prompt_tokens + attestations_tokens
        else:
            # Traditional inline approach - works fine for text content
            # Token count is identical, just different organization
            full_prompt = f"{final_prompt}\n\nAttestations:\n{attestations_text}\n\nOutput the JSON ranking list:"
            message_content = full_prompt
            input_tokens_est = estimate_tokens(full_prompt)
        
        # Warn if input is getting large (approaching 100K tokens)
        if input_tokens_est > 50000:
            logger.warning(f"Large input detected: ~{input_tokens_est:,} tokens. Consider using a model with larger context window.")
        
        # Create message
        if use_file_attachment and isinstance(message_content, list):
            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=max_output_tokens,
                messages=[
                    {"role": "user", "content": message_content}
                ]
            )
        else:
            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=max_output_tokens,
                messages=[
                    {"role": "user", "content": message_content}
                ]
            )
        
        response_text = message.content[0].text
        model_name = message.model
        
        # Get actual token usage if available
        if hasattr(message, 'usage'):
            input_tokens = message.usage.input_tokens
            output_tokens = message.usage.output_tokens
        else:
            input_tokens = input_tokens_est
            output_tokens = estimate_tokens(response_text)
        
        cost_info = estimate_cost_anthropic(input_tokens, output_tokens, model_name)
        
        return (model_name, response_text, cost_info)
    
    except ImportError:
        logger.error("anthropic package not installed")
        return None
    except Exception as e:
        logger.error(f"Anthropic API error: {e}")
        return None


def call_grok(attestations_text: str, prompt: str = RANKING_PROMPT, thinking_mode: bool = False) -> Optional[Tuple[str, str, Dict]]:
    """
    Call Grok API to generate rankings.
    
    Note: Grok API implementation when available.
    
    Args:
        attestations_text: Formatted text of all attestations
        prompt: Prompt template (defaults to RANKING_PROMPT)
        thinking_mode: If True, ask AI to show reasoning process
    
    Returns:
        Tuple of (model_name, response_text, cost_info) or None on error
    """
    # Grok API implementation placeholder
    # This will be implemented when Grok API becomes available
    logger.warning("Grok API not yet implemented")
    return None


def call_deepseek(attestations_text: str, prompt: str = RANKING_PROMPT, thinking_mode: bool = False, use_file_attachment: bool = False) -> Optional[Tuple[str, str, Dict]]:
    """
    Call DeepSeek API to generate rankings.
    
    DeepSeek uses an OpenAI-compatible API, so we can use the OpenAI SDK.
    
    Args:
        attestations_text: Formatted text of all attestations
        prompt: Prompt template (defaults to RANKING_PROMPT)
        thinking_mode: If True, ask AI to show reasoning process
        use_file_attachment: If True, use file attachment (Note: DeepSeek chat completions doesn't support files directly, so this will still be inline)
    
    Returns:
        Tuple of (model_name, response_text, cost_info) or None on error
    """
    try:
        import openai
        
        if not settings.DEEPSEEK_API_KEY:
            logger.error("DeepSeek API key not configured")
            return None
        
        # DeepSeek uses OpenAI-compatible API with a different base URL
        client = openai.OpenAI(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url="https://api.deepseek.com/v1"
        )
        
        # Add thinking mode if requested
        final_prompt = prompt
        if thinking_mode:
            final_prompt = prompt + THINKING_PROMPT_ADDITION
        
        # Note: DeepSeek chat completions API doesn't support file attachments directly
        # If use_file_attachment is True, we'll still include inline but note it in the prompt
        if use_file_attachment:
            full_prompt = f"{final_prompt}\n\nAttestations (attached as text):\n{attestations_text}\n\nOutput the JSON ranking list:"
        else:
            full_prompt = f"{final_prompt}\n\nAttestations:\n{attestations_text}\n\nOutput the JSON ranking list:"
        
        # Estimate tokens for cost calculation
        input_tokens_est = estimate_tokens(full_prompt)
        
        # DeepSeek Chat has a 32K context window
        # Use deepseek-chat model (default) or deepseek-coder for code-related tasks
        model = "deepseek-chat"
        max_output_tokens = 4000 if thinking_mode else 2000
        
        # Check if input is too large
        if input_tokens_est > 30000:
            logger.warning(f"Input tokens ({input_tokens_est:,}) approaching DeepSeek context limit (32K). Consider using a model with larger context window.")
        
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are an expert at evaluating warrior fitness and battle-readiness."},
                {"role": "user", "content": full_prompt}
            ],
            temperature=0.7,
            max_tokens=max_output_tokens
        )
        
        response_text = response.choices[0].message.content
        model_name = response.model
        
        # Get actual token usage if available
        if hasattr(response, 'usage'):
            input_tokens = response.usage.prompt_tokens
            output_tokens = response.usage.completion_tokens
        else:
            input_tokens = input_tokens_est
            output_tokens = estimate_tokens(response_text)
        
        cost_info = estimate_cost_deepseek(input_tokens, output_tokens, model_name)
        
        return (model_name, response_text, cost_info)
    
    except ImportError:
        logger.error("openai package not installed (required for DeepSeek)")
        return None
    except Exception as e:
        logger.error(f"DeepSeek API error: {e}")
        return None


def parse_ranking_response(response_text: str) -> Optional[List[Dict]]:
    """
    Parse JSON ranking response from AI provider.
    
    Handles various JSON formats:
    - Direct array: [{"rank": 1, "name": "..."}, ...]
    - Object with rankings key: {"rankings": [...]}
    - Text with JSON embedded
    
    Args:
        response_text: Raw response text from AI provider
    
    Returns:
        List of ranking dictionaries with 'rank' and 'name' keys, or None on error
    """
    if not response_text:
        return None
    
    try:
        # Try to extract JSON from text if it's embedded
        text = response_text.strip()
        
        # Look for JSON array or object
        start_idx = text.find('[')
        end_idx = text.rfind(']')
        
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_text = text[start_idx:end_idx + 1]
        else:
            # Try object format
            start_idx = text.find('{')
            end_idx = text.rfind('}')
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                json_text = text[start_idx:end_idx + 1]
            else:
                json_text = text
        
        parsed = json.loads(json_text)
        
        # Handle different response formats
        if isinstance(parsed, list):
            rankings = parsed
        elif isinstance(parsed, dict):
            # Try common keys
            if 'rankings' in parsed:
                rankings = parsed['rankings']
            elif 'results' in parsed:
                rankings = parsed['results']
            elif 'list' in parsed:
                rankings = parsed['list']
            else:
                # Assume it's a single ranking object (unlikely but handle it)
                rankings = [parsed]
        else:
            logger.error(f"Unexpected response format: {type(parsed)}")
            return None
        
        # Validate and normalize rankings
        normalized = []
        for item in rankings:
            if isinstance(item, dict):
                rank = item.get('rank') or item.get('position') or item.get('rank_number')
                name = item.get('name') or item.get('warrior') or item.get('participant')
                
                if rank and name:
                    try:
                        normalized.append({
                            'rank': int(rank),
                            'name': str(name).strip()
                        })
                    except (ValueError, TypeError):
                        logger.warning(f"Invalid ranking entry: {item}")
                        continue
        
        if not normalized:
            logger.error("No valid rankings found in response")
            return None
        
        # Sort by rank to ensure correct order
        normalized.sort(key=lambda x: x['rank'])
        
        return normalized
    
    except json.JSONDecodeError as e:
        logger.error(f"JSON parsing error: {e}")
        logger.debug(f"Response text: {response_text[:500]}")
        return None
    except Exception as e:
        logger.error(f"Error parsing ranking response: {e}")
        return None
