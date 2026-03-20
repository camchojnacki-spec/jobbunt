"""AI abstraction layer - supports Anthropic Claude (preferred) and Google Gemini (fallback).

Model Tiers (matched to task complexity):
  - 'flash'    : Cheapest/fastest. Parsing, extraction, simple scoring.
                  Gemini: 2.0-flash  |  Anthropic: claude-haiku-4-5
  - 'balanced' : Good quality, moderate cost. Cover letters, enrichment, Q&A synthesis.
                  Gemini: 2.5-flash  |  Anthropic: claude-sonnet-4-6
  - 'deep'     : Best reasoning. Strategy advisor, career coaching, complex analysis.
                  Gemini: 2.5-pro   |  Anthropic: claude-sonnet-4-6
"""
import os
import json
import re
import logging

logger = logging.getLogger(__name__)

# ── Model mapping ─────────────────────────────────────────────────────────

GEMINI_MODELS = {
    "flash":    "gemini-3.1-flash-lite-preview",
    "balanced": "gemini-2.5-flash",
    "deep":     "gemini-2.5-pro",
    # Legacy aliases
    "fast":     "gemini-3.1-flash-lite-preview",
    "smart":    "gemini-2.5-flash",
}

ANTHROPIC_MODELS = {
    "flash":    "claude-haiku-4-5-20251001",
    "balanced": "claude-sonnet-4-6",
    "deep":     "claude-sonnet-4-6",
    # Legacy aliases
    "fast":     "claude-haiku-4-5-20251001",
    "smart":    "claude-sonnet-4-6",
}

# Thinking budget per tier (only for models that support it)
THINKING_BUDGETS = {
    "flash":    0,       # No thinking needed
    "balanced": 1024,    # Light reasoning
    "deep":     4096,    # Deep reasoning
    "fast":     0,
    "smart":    1024,
}


def get_provider() -> str:
    """Return which AI provider is available: 'anthropic', 'gemini', or 'none'.
    Anthropic is preferred when available (MAX plan)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    return "none"


async def ai_generate(prompt: str, max_tokens: int = 1500, model_tier: str = "fast") -> str:
    """Generate text using whatever AI provider is available.

    model_tier: 'flash' for cheap/quick, 'balanced' for quality, 'deep' for complex reasoning.
    Legacy values 'fast' and 'smart' still work.
    """
    provider = get_provider()

    if provider == "anthropic":
        return await _anthropic_generate(prompt, max_tokens, model_tier)
    elif provider == "gemini":
        return await _gemini_generate(prompt, max_tokens, model_tier)
    else:
        return ""


async def ai_generate_json(prompt: str, max_tokens: int = 1500, model_tier: str = "fast") -> dict | list | None:
    """Generate and parse JSON from AI. Returns None if unavailable or parse fails."""
    text = await ai_generate(prompt, max_tokens, model_tier)
    if not text:
        return None

    # Strip markdown code blocks if present
    cleaned = text.strip()

    # Try to extract content between ```json ... ``` fences first (most reliable for Gemini)
    fence_match = re.search(r"```(?:json)?\s*\n([\s\S]*?)\n\s*```", cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    else:
        # Fallback: strip leading/trailing fences
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        cleaned = cleaned.strip()

    # Try direct parse first (cleanest path)
    for attempt_text in [cleaned, text.strip()]:
        try:
            return json.loads(attempt_text)
        except (json.JSONDecodeError, ValueError):
            pass

    # Try extracting JSON structures
    try:
        # Try object extraction first (more common for our use cases)
        json_match = re.search(r"(\{[\s\S]*\})", cleaned)
        if json_match:
            return json.loads(json_match.group(1))
    except (json.JSONDecodeError, ValueError):
        pass

    try:
        # Try array extraction
        json_match = re.search(r"(\[[\s\S]*\])", cleaned)
        if json_match:
            parsed = json.loads(json_match.group(1))
            if isinstance(parsed, list):
                return parsed
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"JSON parse failed: {e}\nRaw text: {text[:500]}")

    return None


async def _anthropic_generate(prompt: str, max_tokens: int, model_tier: str) -> str:
    """Use Anthropic Claude API."""
    try:
        import anthropic
        client = anthropic.AsyncAnthropic()
        model = ANTHROPIC_MODELS.get(model_tier, ANTHROPIC_MODELS["flash"])
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception as e:
        logger.error(f"Anthropic API error: {e}")
        return ""


async def _gemini_generate(prompt: str, max_tokens: int, model_tier: str) -> str:
    """Use Google Gemini API with tier-appropriate model."""
    try:
        from google import genai

        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        client = genai.Client(api_key=api_key)

        model_name = GEMINI_MODELS.get(model_tier, GEMINI_MODELS["flash"])
        thinking_budget = THINKING_BUDGETS.get(model_tier, 0)

        config = {"max_output_tokens": max_tokens}

        # Thinking models (2.5 Flash, 2.5 Pro) need higher max_output_tokens because
        # thinking tokens count against the budget. Set generous minimums per tier.
        if thinking_budget > 0:
            config["thinking_config"] = {"thinking_budget": thinking_budget}
            # Deep tier (2.5 Pro) needs the most headroom
            if model_tier == "deep":
                config["max_output_tokens"] = max(max_tokens, 8192)
            else:
                config["max_output_tokens"] = max(max_tokens, 4096)

        logger.info(f"Gemini call: model={model_name}, tier={model_tier}, max_tokens={config['max_output_tokens']}, thinking={thinking_budget}")

        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=config,
        )

        # Handle thinking models that may return None text on truncation
        if response.text is None:
            finish = response.candidates[0].finish_reason if response.candidates else "unknown"
            logger.warning(f"Gemini returned None text (finish_reason={finish}), tier={model_tier}")
            return ""
        return response.text
    except Exception as e:
        logger.error(f"Gemini API error ({model_tier}): {e}")
        return ""
