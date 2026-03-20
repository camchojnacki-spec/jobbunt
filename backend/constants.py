"""Shared constants for the Jobbunt backend."""

# ── Seniority tier definitions ────────────────────────────────────────
SENIORITY_TIERS = ["entry", "mid", "senior", "director", "vp", "c-suite"]

TIER_TITLE_KEYWORDS = {
    "entry": ["entry", "junior", "associate", "intern"],
    "mid": ["mid", "intermediate"],
    "senior": ["senior", "sr.", "principal", "staff"],
    "director": ["director", "head of", "practice lead"],
    "vp": ["vp", "vice president", "avp", "svp"],
    "c-suite": ["chief", "cto", "cio", "ciso", "cfo", "coo", "ceo"],
}

# Variant keywords used by the scraper for generating seniority-based role variants
TIER_TITLE_VARIANTS = {
    "entry": ["junior", "associate", "analyst", "coordinator"],
    "mid": ["specialist", "consultant", "advisor", "lead"],
    "senior": ["senior", "sr.", "principal", "staff"],
    "director": ["director", "head of", "practice lead"],
    "vp": ["vp", "vice president", "avp", "svp"],
    "c-suite": ["chief", "cto", "cio", "ciso", "cfo", "coo"],
}

# Title synonyms: expanded forms for abbreviations commonly used in titles
TITLE_SYNONYMS = {
    "ciso": "chief information security officer",
    "cto": "chief technology officer",
    "cio": "chief information officer",
    "cfo": "chief financial officer",
    "coo": "chief operating officer",
    "ceo": "chief executive officer",
    "vp": "vice president",
    "svp": "senior vice president",
    "avp": "assistant vice president",
    "evp": "executive vice president",
}

# ── Job statuses ──────────────────────────────────────────────────────
STATUS_PENDING = "pending"
STATUS_LIKED = "liked"
STATUS_PASSED = "passed"
STATUS_SHORTLISTED = "shortlisted"

# ── Application pipeline statuses ─────────────────────────────────────
PIPELINE_APPLIED = "applied"
PIPELINE_SCREENING = "screening"
PIPELINE_INTERVIEW = "interview"
PIPELINE_OFFER = "offer"
PIPELINE_ACCEPTED = "accepted"
PIPELINE_REJECTED = "rejected"
PIPELINE_NO_RESPONSE = "no_response"
