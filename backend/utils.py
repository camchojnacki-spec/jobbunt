"""Shared utility helpers for the Jobbunt backend."""
import json
import logging

logger = logging.getLogger(__name__)


def safe_json(raw, default=None):
    """Parse a JSON string safely, returning *default* on any failure."""
    if default is None:
        default = []
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning(f"Malformed JSON field (len={len(raw)}): {raw[:80]}...")
        return default


def safe_json_list(val) -> list:
    """Safely parse a JSON list field, returning [] on any error."""
    if not val:
        return []
    try:
        result = json.loads(val)
        return result if isinstance(result, list) else [str(result)]
    except (json.JSONDecodeError, TypeError):
        # Field contains plain text (e.g. from AI suggestion) — wrap as single-item list
        return [s.strip().strip('"') for s in val.split(",") if s.strip()] if val else []
