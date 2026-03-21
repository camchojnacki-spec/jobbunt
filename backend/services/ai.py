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
import hashlib
import datetime
import logging

logger = logging.getLogger(__name__)

# ── Lazy singleton clients (avoid re-creating on every call) ──────────────

_anthropic_client = None
_gemini_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.AsyncAnthropic()
    return _anthropic_client


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


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


# ── AI Cache helpers ──────────────────────────────────────────────────────

# TTL defaults per model tier (hours)
_CACHE_TTL = {
    "flash":    24,
    "fast":     24,
    "balanced": 24,
    "smart":    24,
    "deep":     1,    # Market-sensitive / complex reasoning — shorter TTL
}


def _cache_key(prompt: str, model_tier: str) -> str:
    """SHA-256 hash of prompt + model_tier for cache lookup."""
    raw = f"{model_tier}:{prompt}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> str | None:
    """Look up a non-expired cached response. Returns None on miss."""
    from backend.database import SessionLocal
    from backend.models.models import AICache
    db = SessionLocal()
    try:
        row = db.query(AICache).filter(AICache.cache_key == key).first()
        if row is None:
            return None
        expires_at = row.created_at + datetime.timedelta(hours=row.ttl_hours)
        if datetime.datetime.utcnow() > expires_at:
            # Expired — delete and treat as miss
            db.delete(row)
            db.commit()
            return None
        return row.response
    except Exception as e:
        logger.debug(f"AI cache read error: {e}")
        return None
    finally:
        db.close()


def _cache_put(key: str, response: str, model_tier: str, ttl_hours: int) -> None:
    """Store a response in the cache (upsert)."""
    from backend.database import SessionLocal
    from backend.models.models import AICache
    db = SessionLocal()
    try:
        row = db.query(AICache).filter(AICache.cache_key == key).first()
        if row:
            row.response = response
            row.model_tier = model_tier
            row.ttl_hours = ttl_hours
            row.created_at = datetime.datetime.utcnow()
        else:
            row = AICache(
                cache_key=key,
                response=response,
                model_tier=model_tier,
                ttl_hours=ttl_hours,
                created_at=datetime.datetime.utcnow(),
            )
            db.add(row)
        db.commit()
    except Exception as e:
        logger.debug(f"AI cache write error: {e}")
        db.rollback()
    finally:
        db.close()


async def ai_generate(prompt: str, max_tokens: int = 1500, model_tier: str = "fast",
                      use_cache: bool = True) -> str:
    """Generate text using whatever AI provider is available.

    model_tier: 'flash' for cheap/quick, 'balanced' for quality, 'deep' for complex reasoning.
    Legacy values 'fast' and 'smart' still work.
    use_cache: When True (default), check/store results in the ai_cache table.
    """
    # ── Cache lookup ──────────────────────────────────────────────────
    key = _cache_key(prompt, model_tier) if use_cache else None
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            logger.debug(f"AI cache hit for tier={model_tier} key={key[:12]}…")
            return cached

    # ── Generate ──────────────────────────────────────────────────────
    provider = get_provider()

    if provider == "anthropic":
        result = await _anthropic_generate(prompt, max_tokens, model_tier)
    elif provider == "gemini":
        result = await _gemini_generate(prompt, max_tokens, model_tier)
    else:
        return ""

    # ── Cache store (only non-empty responses) ────────────────────────
    if use_cache and result:
        ttl = _CACHE_TTL.get(model_tier, 24)
        _cache_put(key, result, model_tier, ttl)

    return result


async def ai_generate_json(prompt: str, max_tokens: int = 1500, model_tier: str = "fast",
                           use_cache: bool = True) -> dict | list | None:
    """Generate and parse JSON from AI. Returns None if unavailable or parse fails."""
    text = await ai_generate(prompt, max_tokens, model_tier, use_cache=use_cache)
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
        client = _get_anthropic_client()
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
        client = _get_gemini_client()

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

        response = await client.aio.models.generate_content(
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
