"""Multi-dimensional job scoring engine.

Scores jobs against a candidate profile across 6 dimensions:
- Role Fit: How well the title/responsibilities match target roles
- Skills Match: Overlap between candidate skills and job requirements
- Location: Geographic and remote work alignment
- Compensation: Salary range vs expectations
- Seniority: Level alignment with candidate experience
- Culture Fit: Company culture alignment with candidate values/preferences
"""
import json
import logging
import re
from typing import Optional

from backend.models.models import Job, Profile, Company
from backend.services.ai import ai_generate_json, get_provider

logger = logging.getLogger(__name__)

# ── Seniority tier definitions (mirrored from scraper) ──────────────
SENIORITY_TIERS = ["entry", "mid", "senior", "director", "vp", "c-suite"]

TIER_TITLE_KEYWORDS = {
    "entry": ["entry", "junior", "associate", "intern"],
    "mid": ["mid", "intermediate"],
    "senior": ["senior", "sr.", "principal", "staff"],
    "director": ["director", "head of", "practice lead"],
    "vp": ["vp", "vice president", "avp", "svp"],
    "c-suite": ["chief", "cto", "cio", "ciso", "cfo", "coo", "ceo"],
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


def _expand_title(title: str) -> str:
    """Expand abbreviations in a job title for better matching.

    e.g. 'CISO' -> 'Chief Information Security Officer CISO'
    """
    title_lower = title.lower()
    expanded = title
    for abbr, full_form in TITLE_SYNONYMS.items():
        # If the abbreviation appears as a word boundary match, append the full form
        if _word_match(abbr, title_lower) and full_form.lower() not in title_lower:
            expanded = f"{expanded} {full_form}"
        # Also: if the full form is in the title, append the abbreviation
        elif full_form.lower() in title_lower and not _word_match(abbr, title_lower):
            expanded = f"{expanded} {abbr}"
    return expanded


def _detect_job_seniority_tier(title: str) -> str:
    """Detect the seniority tier of a job title based on keywords.

    Returns the tier string or empty string if unknown.
    """
    title_lower = title.lower()
    # Check from highest to lowest (c-suite first, then vp, etc.)
    for tier in reversed(SENIORITY_TIERS):
        keywords = TIER_TITLE_KEYWORDS.get(tier, [])
        for kw in keywords:
            if _word_match(kw, title_lower):
                return tier
    return ""


def _is_within_tier_range(candidate_tier: str, job_tier: str, tiers_up: int, tiers_down: int = 0) -> bool:
    """Check if a job's tier falls within the candidate's search range."""
    if candidate_tier not in SENIORITY_TIERS or job_tier not in SENIORITY_TIERS:
        return False
    c_idx = SENIORITY_TIERS.index(candidate_tier)
    j_idx = SENIORITY_TIERS.index(job_tier)
    return (c_idx - tiers_down) <= j_idx <= (c_idx + tiers_up)


def _extract_domain_words(roles: list[str]) -> set:
    """Extract the domain/function words from target roles, ignoring generic seniority words."""
    generic = {
        "director", "senior", "manager", "head", "lead", "chief", "vp",
        "vice", "president", "associate", "junior", "principal", "staff",
        "executive", "officer", "analyst", "specialist", "consultant",
        "advisor", "coordinator", "administrator", "engineer", "architect",
        "of", "and", "the", "for",
    }
    words = set()
    for role in roles:
        cleaned = re.sub(r'[,\-()&/]', ' ', role.lower())
        for w in cleaned.split():
            if len(w) > 2 and w not in generic:
                words.add(w)
    return words


def _word_match(term, text):
    """Word-boundary aware matching to prevent 'cloud' matching 'cloudflare'."""
    pattern = r'\b' + re.escape(term.lower()) + r'\b'
    return bool(re.search(pattern, text.lower()))


def _safe_json(raw, default=None):
    """Parse a JSON string safely, returning *default* on any failure."""
    if default is None:
        default = []
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


# Dimension weights (must sum to 100)
# Base weights used when deep research is NOT available
BASE_WEIGHTS = {
    "role_fit": 35,
    "skills": 15,
    "location": 15,
    "compensation": 15,
    "seniority": 10,
    "culture_fit": 10,
}

# Weights used when deep research IS available (research_fit gets weight, others shrink)
RESEARCH_WEIGHTS = {
    "role_fit": 30,
    "skills": 14,
    "location": 12,
    "compensation": 12,
    "seniority": 8,
    "culture_fit": 8,
    "research_fit": 16,
}

# Default weights alias (for backward compat)
WEIGHTS = BASE_WEIGHTS

DIMENSION_LABELS = {
    "role_fit": "Role Fit",
    "skills": "Skills Match",
    "location": "Location",
    "compensation": "Compensation",
    "seniority": "Seniority",
    "culture_fit": "Culture Fit",
    "research_fit": "Deep Research",
}


def score_job_multidim(job: Job, profile: Profile, company: Company = None) -> dict:
    """Score a job across multiple dimensions. Returns full breakdown.

    Calibrated for real variability: most jobs should land 45-75 overall.
    Missing data (no salary, no company info) scores neutral (50), not penalized.
    80+ is genuinely strong. 90+ is rare and exceptional.
    """
    breakdown = {}
    reasons = []

    skills = _safe_json(profile.skills, [])
    target_roles = _safe_json(profile.target_roles, [])
    target_locations = _safe_json(profile.target_locations, [])
    deal_breakers = _safe_json(profile.deal_breakers, [])
    values = _safe_json(profile.values, [])

    job_text = f"{job.title} {job.description or ''} {job.requirements or ''}".lower()

    # ── Domain Mismatch Detection ────────────────────────────────────
    domain_match = True
    title_lower = (job.title or "").lower()

    # Define domain-mismatch rules: (profile_domain_keywords, job_mismatch_keywords)
    _DOMAIN_MISMATCH_RULES = [
        # IT/Cyber security profile vs physical security jobs
        (
            ["cybersecurity", "information security", "it security", "infosec",
             "cyber security", "ciso", "soc analyst", "penetration test",
             "security engineer", "security architect", "devsecops"],
            ["physical security", "loss prevention", "security guard",
             "patrol", "armed", "unarmed", "cctv operator", "surveillance officer",
             "bodyguard", "bouncer", "fire watch", "gate guard",
             "security officer", "guard supervisor", "protective services",
             "asset protection", "door supervisor", "access control officer",
             "building security", "site security", "mobile patrol"],
        ),
        # Information security (broader match)
        (
            ["information security"],
            ["physical security", "security guard", "loss prevention",
             "asset protection", "security officer", "patrol",
             "surveillance officer", "access control officer"],
        ),
        # Cybersecurity (broader match)
        (
            ["cybersecurity"],
            ["physical security", "security guard", "loss prevention",
             "armed guard", "unarmed guard", "door supervisor"],
        ),
        # IT security (broader match)
        (
            ["it security"],
            ["physical security", "security guard", "building security",
             "site security", "mobile patrol"],
        ),
        # Software engineering vs unrelated "engineer" roles
        (
            ["software engineer", "software developer", "full stack",
             "frontend developer", "backend developer", "web developer"],
            ["mechanical engineer", "civil engineer", "chemical engineer",
             "electrical engineer", "structural engineer", "hvac engineer",
             "field engineer", "plant engineer", "maintenance engineer"],
        ),
        # Data science vs unrelated roles
        (
            ["data scientist"],
            ["political scientist", "social scientist", "research scientist",
             "data entry", "data entry clerk", "data capture"],
        ),
        # Data/analytics vs unrelated roles
        (
            ["data analyst", "data engineer",
             "machine learning", "business intelligence"],
            ["data entry", "data entry clerk", "data capture"],
        ),
    ]

    # Titles that should NEVER be flagged as physical security domain mismatch
    # (they contain words like "security officer" but are clearly IT/cyber roles)
    _CYBER_EXECUTIVE_PATTERNS = [
        "chief information security officer",
        "chief security officer",
        "ciso",
        "information security officer",
        "it security officer",
        "cyber security officer",
        "cybersecurity officer",
        "data security officer",
        "data protection officer",
    ]

    # Check if any profile target roles or skills match a domain, then check job title
    profile_domain_text = " ".join(target_roles + skills).lower()
    for domain_keywords, mismatch_keywords in _DOMAIN_MISMATCH_RULES:
        profile_in_domain = any(_word_match(kw, profile_domain_text) for kw in domain_keywords)
        if profile_in_domain:
            # Before flagging mismatch, check if the title is actually a cyber executive role
            is_cyber_exec = any(pat in title_lower for pat in _CYBER_EXECUTIVE_PATTERNS)
            if is_cyber_exec:
                continue  # Don't flag CISO-type roles as physical security
            job_is_mismatch = any(_word_match(kw, title_lower) for kw in mismatch_keywords)
            if job_is_mismatch:
                domain_match = False
                break

    # ── Role Fit (0-100) ─────────────────────────────────────────────
    role_score = 0
    best_role_match = ""

    # If domain mismatch detected, cap role_fit very low
    if not domain_match:
        role_score = 5
        reasons.append(f"Domain mismatch: '{job.title}' is outside target field")
        breakdown["role_fit"] = role_score
        breakdown["domain_match"] = False
        # Skip normal role_fit calculation — jump to skills
    else:
        breakdown["domain_match"] = True

    # Generic title words that shouldn't drive role matching
    GENERIC_TITLE_WORDS = {
        "director", "senior", "manager", "head", "lead", "chief", "vp",
        "vice", "president", "associate", "junior", "principal", "staff",
        "executive", "officer", "analyst", "specialist", "consultant",
        "advisor", "coordinator", "administrator", "engineer", "architect",
    }

    if domain_match:
        # Expand abbreviations in the job title for better matching
        expanded_title = _expand_title(job.title or "")
        expanded_title_lower = expanded_title.lower()
        expanded_job_text = f"{expanded_title} {job.description or ''} {job.requirements or ''}".lower()

        for role in target_roles:
            role_words = [w for w in role.lower().split() if len(w) > 2]
            if not role_words:
                continue
            title_lower = expanded_title_lower

            # Separate distinctive words from generic seniority/title words
            distinctive_words = [w for w in role_words if w not in GENERIC_TITLE_WORDS]
            generic_words = [w for w in role_words if w in GENERIC_TITLE_WORDS]

            if _word_match(role.lower(), title_lower):
                # Exact match in title is strong but not 100 (could be wrong context/level)
                score = 85
                # Boost if description also confirms
                desc_words = [w for w in role_words if _word_match(w, expanded_job_text)]
                if len(desc_words) == len(role_words):
                    score = 92
                role_score = max(role_score, score)
                best_role_match = role
            else:
                # Count matches separately for distinctive vs generic words
                dist_title_matches = sum(1 for w in distinctive_words if _word_match(w, title_lower)) if distinctive_words else 0
                gen_title_matches = sum(1 for w in generic_words if _word_match(w, title_lower)) if generic_words else 0
                desc_matches = sum(1 for w in role_words if _word_match(w, expanded_job_text))
                desc_ratio = desc_matches / len(role_words) if role_words else 0

                # Distinctive words in title are what really matter
                dist_ratio = dist_title_matches / len(distinctive_words) if distinctive_words else 0
                # Generic words (director, senior, etc.) provide minor bonus only
                gen_bonus = min(gen_title_matches * 3, 10)

                if dist_ratio >= 0.8:
                    # Most distinctive words match in title — strong fit
                    combined = 60 + (dist_ratio * 20) + gen_bonus
                elif dist_ratio >= 0.5:
                    combined = 40 + (dist_ratio * 25) + gen_bonus + (desc_ratio * 10)
                elif dist_ratio > 0:
                    # Some distinctive words match
                    combined = 15 + (dist_ratio * 30) + gen_bonus + (desc_ratio * 10)
                elif gen_title_matches > 0 and desc_ratio >= 0.5:
                    # Only generic words match in title but description has good coverage
                    combined = 15 + (desc_ratio * 20) + gen_bonus
                else:
                    combined = (desc_ratio * 15) + gen_bonus

                if combined > role_score:
                    role_score = combined
                    best_role_match = role

        # ── Seniority Tier Expansion Boost ────────────────────────────
        # When a profile has search_tiers_up > 0, jobs at higher seniority
        # tiers in the SAME domain should be scored as highly relevant.
        tiers_up = getattr(profile, 'search_tiers_up', 0) or 0
        tiers_down = getattr(profile, 'search_tiers_down', 0) or 0
        candidate_tier = (profile.seniority_level or "").lower()

        if (tiers_up > 0 or tiers_down > 0) and candidate_tier and target_roles:
            job_tier = _detect_job_seniority_tier(expanded_title)
            if job_tier and job_tier != candidate_tier:
                in_range = _is_within_tier_range(candidate_tier, job_tier, tiers_up, tiers_down)
                # Also check "near miss" — 1 tier beyond the explicit range
                near_miss = (not in_range and
                             (_is_within_tier_range(candidate_tier, job_tier, tiers_up + 1, tiers_down + 1)))

                if in_range or near_miss:
                    # Check domain overlap: do the domain words from target roles
                    # appear in the job title?
                    profile_domain = _extract_domain_words(target_roles)
                    title_domain = _extract_domain_words([expanded_title])

                    domain_overlap = profile_domain & title_domain
                    if profile_domain:
                        domain_ratio = len(domain_overlap) / len(profile_domain)
                    else:
                        domain_ratio = 0

                    # Also check domain words against the full job text
                    domain_in_text = sum(1 for dw in profile_domain if _word_match(dw, expanded_job_text))
                    text_domain_ratio = domain_in_text / len(profile_domain) if profile_domain else 0

                    if domain_ratio >= 0.25 or text_domain_ratio >= 0.3:
                        if in_range:
                            # Same domain, desired tier — strong match
                            tier_boost_base = 70
                            domain_bonus = min(domain_ratio * 20, 20)
                            text_bonus = min(text_domain_ratio * 10, 10)
                        else:
                            # Near miss — same domain, 1 tier beyond range — moderate match
                            tier_boost_base = 60
                            domain_bonus = min(domain_ratio * 15, 15)
                            text_bonus = min(text_domain_ratio * 8, 8)

                        tier_role_score = tier_boost_base + domain_bonus + text_bonus

                        if tier_role_score > role_score:
                            role_score = tier_role_score
                            label = "tier-expanded match" if in_range else "near-tier match"
                            best_role_match = f"{job.title} ({label})"
                            if in_range:
                                reasons.append(f"Seniority tier match: {job_tier} level (search_tiers_up={tiers_up})")
                            else:
                                reasons.append(f"Near seniority tier: {job_tier} level")

        if not target_roles:
            role_score = 50  # no target = neutral

        breakdown["role_fit"] = min(round(role_score), 100)
        if role_score >= 50 and best_role_match:
            reasons.append(f"Role aligns with: {best_role_match}")

    # ── Skills Match (0-100) ─────────────────────────────────────────
    skills_score = 0
    if skills:
        matched = []
        for skill in skills:
            skill_lower = skill.lower()
            if len(skill_lower) >= 4 and _word_match(skill_lower, job_text):
                matched.append(skill)
            elif len(skill_lower) > 6:
                words = [w for w in skill_lower.split() if len(w) > 3]
                if len(words) >= 2 and sum(1 for w in words if _word_match(w, job_text)) >= 2:
                    matched.append(skill)

        if matched:
            ratio = len(matched) / len(skills)
            # Adjust for large skill lists: matching 8/25 (32%) is more impressive
            # than matching 2/6 (33%) — use absolute count as a floor boost
            count_bonus = min(len(matched) * 2, 15) if len(skills) >= 10 else 0
            if ratio >= 0.8:
                skills_score = 82 + round((ratio - 0.8) * 90)   # 82-100
            elif ratio >= 0.5:
                skills_score = 60 + round((ratio - 0.5) * 73)   # 60-82
            elif ratio >= 0.3:
                skills_score = 40 + round((ratio - 0.3) * 100)  # 40-60
            elif ratio >= 0.15:
                skills_score = 25 + round((ratio - 0.15) * 100) # 25-40
            else:
                skills_score = round(ratio * 167)                # 0-25
            skills_score = min(skills_score + count_bonus, 100)
            top_skills = matched[:4]
            reasons.append(f"Skills: {', '.join(top_skills)} ({len(matched)}/{len(skills)})")
        else:
            skills_score = 10  # listed skills but zero matches = bad
    else:
        skills_score = 50  # no skills listed = neutral

    breakdown["skills"] = min(skills_score, 100)

    # ── Location (0-100) ─────────────────────────────────────────────
    loc_score = 50  # unknown location = neutral
    if target_locations and job.location:
        loc_lower = job.location.lower()
        matched_loc = False
        for loc in target_locations:
            if loc.lower() in loc_lower or loc_lower in loc.lower():
                loc_score = 82
                reasons.append(f"Location: {job.location}")
                matched_loc = True
                break
        if not matched_loc:
            loc_score = 15  # wrong location

    remote_type = (job.remote_type or "").lower()
    if profile.remote_preference == "remote":
        if remote_type == "remote":
            loc_score = max(loc_score, 88)
            reasons.append("Fully remote")
        elif remote_type == "hybrid":
            loc_score = max(loc_score, 50)
            reasons.append("Hybrid (prefers remote)")
        elif remote_type == "onsite":
            loc_score = min(loc_score, 20)
    elif profile.remote_preference == "hybrid":
        if remote_type == "hybrid":
            loc_score = max(loc_score, 82)
        elif remote_type == "remote":
            loc_score = max(loc_score, 78)
    elif profile.remote_preference == "any":
        if remote_type == "remote":
            loc_score = min(loc_score + 10, 90)
            reasons.append("Remote available")

    if not job.location and not remote_type:
        loc_score = 50  # no location info = neutral, don't penalize

    breakdown["location"] = min(loc_score, 100)

    # ── Compensation (0-100) ──────────────────────────────────────────
    # Level-aware: considers whether salary is appropriate for the seniority
    comp_score = 50  # no salary info = neutral (don't penalize unlisted salary)
    if profile.min_salary and (job.salary_min or job.salary_max):
        job_max = job.salary_max or job.salary_min or 0
        job_min = job.salary_min or job.salary_max or 0
        target_min = profile.min_salary
        target_max = profile.max_salary or round(target_min * 1.3)
        target_mid = (target_min + target_max) / 2

        if job_min >= target_mid:
            comp_score = 78
            reasons.append(f"Salary strong: ${job_min:,}-${job_max:,}")
            if job_min >= target_max:
                comp_score = 88
        elif job_max >= target_mid:
            comp_score = 62
            reasons.append(f"Salary adequate: ${job_min:,}-${job_max:,}")
        elif job_max >= target_min:
            comp_score = 42
            reasons.append(f"Salary at low end: ${job_min:,}-${job_max:,}")
        elif job_max >= target_min * 0.85:
            comp_score = 28
            reasons.append("Salary below target")
        else:
            comp_score = 10
            reasons.append("Salary well below target")

        # Level-appropriateness penalty
        level_map_comp = {"entry": 1, "mid": 2, "senior": 3, "director": 4, "vp": 5, "c-suite": 6}
        c_lvl = level_map_comp.get((profile.seniority_level or "").lower(), 0)
        j_lvl = level_map_comp.get((job.seniority_level or "").lower(), 0)
        if c_lvl >= 4 and j_lvl >= 4 and job_max < target_min:
            comp_score = max(comp_score - 15, 5)  # senior underpay = bigger hit

    elif job.salary_estimated:
        comp_score = 40  # estimated salary = slight uncertainty

    breakdown["compensation"] = comp_score

    # ── Seniority (0-100) ─────────────────────────────────────────────
    seniority_score = 50  # unknown = neutral
    level_map = {"entry": 1, "mid": 2, "senior": 3, "director": 4, "vp": 5, "c-suite": 6}

    candidate_level = level_map.get(
        (profile.seniority_level or "").lower(), 0
    )
    if not candidate_level and profile.experience_years:
        if profile.experience_years >= 15:
            candidate_level = 5
        elif profile.experience_years >= 10:
            candidate_level = 4
        elif profile.experience_years >= 6:
            candidate_level = 3
        elif profile.experience_years >= 3:
            candidate_level = 2
        else:
            candidate_level = 1

    # Use stored seniority_level, or infer from job title if not set
    job_seniority = (job.seniority_level or "").lower()
    if not job_seniority and job.title:
        job_seniority = _detect_job_seniority_tier(job.title)
    job_level = level_map.get(job_seniority, 0)

    # Account for search_tiers_up/down in seniority scoring
    tiers_up_val = getattr(profile, 'search_tiers_up', 0) or 0
    tiers_down_val = getattr(profile, 'search_tiers_down', 0) or 0

    if candidate_level and job_level:
        diff = candidate_level - job_level  # positive = overqualified
        if diff == 0:
            seniority_score = 82
            reasons.append(f"Level match: {job.seniority_level}")
        elif diff == -1:
            # Job is 1 level above candidate
            if tiers_up_val >= 1:
                # Candidate explicitly wants to see higher-tier roles
                seniority_score = 78
                reasons.append(f"Desired stretch: {job.seniority_level} level")
            else:
                seniority_score = 62  # stretch up
                reasons.append(f"Stretch: {job.seniority_level} level")
        elif diff == -2:
            if tiers_up_val >= 2:
                seniority_score = 72
                reasons.append(f"Desired stretch: {job.seniority_level} level")
            elif tiers_up_val >= 1:
                # Job is 2 levels up but candidate wants at least 1 up — moderate stretch
                seniority_score = 58
                reasons.append(f"Ambitious stretch: {job.seniority_level} level")
            else:
                seniority_score = 32  # big stretch
        elif diff == 1:
            if tiers_down_val >= 1:
                seniority_score = 68
                reasons.append(f"Within tier range: {job.seniority_level} level")
            else:
                seniority_score = 48  # slightly overqualified
                reasons.append("Below target seniority")
        elif diff >= 2:
            seniority_score = 18  # way overqualified
            reasons.append("Well below target seniority")
        else:
            seniority_score = 22

    breakdown["seniority"] = seniority_score

    # ── Culture Fit (0-100) ───────────────────────────────────────────
    culture_score = 50  # no info = neutral
    if company and values:
        company_signals = " ".join(filter(None, [
            company.culture_summary,
            company.description,
            " ".join(json.loads(company.pros or "[]")),
        ])).lower()

        if company_signals:
            value_matches = sum(1 for v in values if _word_match(v, company_signals))
            if value_matches >= 3:
                culture_score = min(52 + (value_matches * 10), 82)
                reasons.append(f"Culture aligns with {value_matches} values")
            elif value_matches > 0:
                culture_score = 45 + (value_matches * 8)
                reasons.append(f"Some culture alignment ({value_matches} values)")
            else:
                culture_score = 35

        if company.glassdoor_rating:
            if company.glassdoor_rating >= 4.2:
                culture_score = min(culture_score + 12, 88)
            elif company.glassdoor_rating >= 3.8:
                culture_score = min(culture_score + 6, 82)
            elif company.glassdoor_rating < 3.0:
                culture_score = max(culture_score - 15, 10)
                reasons.append(f"Low Glassdoor: {company.glassdoor_rating}")

        if company.score_overall:
            culture_score = round(culture_score * 0.7 + company.score_overall * 0.3)

    if deal_breakers:
        for db_item in deal_breakers:
            if _word_match(db_item, job_text):
                culture_score = max(culture_score - 25, 0)
                reasons.append(f"Deal breaker: {db_item}")

    breakdown["culture_fit"] = min(max(culture_score, 0), 100)

    # ── Deep Research Fit (0-100) ─────────────────────────────────────
    # Only scored when deep research data exists
    has_research = bool(job.deep_researched and (job.culture_insights or job.hiring_sentiment))
    active_weights = RESEARCH_WEIGHTS if has_research else BASE_WEIGHTS

    if has_research:
        research_score = _score_deep_research(job, profile)
        breakdown["research_fit"] = research_score
        if research_score >= 70:
            reasons.append("Deep research: strong fit signals")
        elif research_score <= 35:
            reasons.append("Deep research: some concerns noted")

    # ── Advisor Boost ──────────────────────────────────────────────────
    # If the AI advisor has recommended roles/keywords, boost matching jobs
    advisor_bonus = 0
    advisor_data = _safe_json(getattr(profile, 'advisor_data', None), {})
    if isinstance(advisor_data, dict):
        # Boost if job title matches advisor-recommended roles
        advisor_roles = advisor_data.get("roles_to_consider", [])
        if advisor_roles and job.title:
            title_lower = job.title.lower()
            for arole in advisor_roles:
                arole_words = [w for w in arole.lower().split() if len(w) > 2]
                if not arole_words:
                    continue
                hits = sum(1 for w in arole_words if _word_match(w, title_lower))
                if hits / len(arole_words) >= 0.5:
                    advisor_bonus = max(advisor_bonus, 4)
                    reasons.append(f"Advisor-recommended role: {arole}")
                    break

        # Boost if job mentions advisor-recommended ATS keywords
        ats_keywords = advisor_data.get("keywords_for_ats", [])
        if ats_keywords:
            ats_hits = sum(1 for kw in ats_keywords if _word_match(kw, job_text))
            if ats_hits >= 3:
                advisor_bonus += 2
            elif ats_hits >= 1:
                advisor_bonus += 1

        # Boost if job is at a company the advisor targeted
        target_companies = advisor_data.get("companies_to_target", [])
        if target_companies and job.company:
            company_lower = job.company.lower()
            for tc in target_companies:
                if tc.lower() in company_lower or company_lower in tc.lower():
                    advisor_bonus += 3
                    reasons.append(f"Advisor-targeted company: {tc}")
                    break

        # Boost if job is in an advisor-recommended industry
        target_industries = advisor_data.get("industry_targets", [])
        if target_industries and job_text:
            ind_hits = sum(1 for ind in target_industries if _word_match(ind, job_text))
            if ind_hits >= 1:
                advisor_bonus += 2

    # ── Calculate weighted overall score ──────────────────────────────
    overall = sum(
        breakdown.get(dim, 50) * (active_weights[dim] / 100)
        for dim in active_weights
    ) + advisor_bonus

    # Cap overall score when domain mismatch is detected
    if not domain_match:
        overall = min(overall, 15)

    # ── Role-alignment gate ─────────────────────────────────────────
    # If role_fit is very low, the job title doesn't match target roles.
    # Skills alone shouldn't carry a job to a high score — cap it.
    role_fit_val = breakdown.get("role_fit", 50)
    if target_roles and role_fit_val < 25:
        # Title barely matches any target role — cap score to prevent
        # skill-only matches from ranking high (e.g., "Risk Manager" when
        # target is "Director, Information Security")
        overall = min(overall, 45)
        if role_fit_val < 15:
            overall = min(overall, 35)
            reasons.append("Title doesn't match target roles")

    # Clamp to 0-100
    overall = max(0, min(100, overall))

    return {
        "score": round(overall, 1),
        "breakdown": breakdown,
        "reasons": reasons,
        "weights": active_weights,
    }


def _build_research_context(job: Job) -> str:
    """Build deep research context string for AI scoring prompts."""
    if not job.deep_researched:
        return ""
    parts = ["DEEP RESEARCH FINDINGS (factor these into your scoring, especially culture_fit and research_fit):"]
    if job.culture_insights:
        parts.append(f"Culture: {job.culture_insights[:300]}")
    if job.growth_opportunities:
        parts.append(f"Growth: {job.growth_opportunities[:200]}")
    if job.hiring_sentiment:
        parts.append(f"Hiring Climate: {job.hiring_sentiment[:200]}")
    if job.day_in_life:
        parts.append(f"Day-to-Day: {job.day_in_life[:200]}")
    return "\n".join(parts)


def _score_deep_research(job: Job, profile: Profile) -> int:
    """Score 0-100 based on deep research findings (culture, growth, hiring sentiment).
    Uses keyword/sentiment heuristics since this runs synchronously."""
    score = 50  # neutral baseline

    positive_signals = [
        "strong", "excellent", "thriving", "innovative", "collaborative",
        "growth", "opportunity", "competitive", "supportive", "empowering",
        "dynamic", "rewarding", "promising", "expanding", "investing",
        "high demand", "actively hiring", "growing team", "good culture",
    ]
    negative_signals = [
        "toxic", "burnout", "high turnover", "restructuring", "layoffs",
        "stagnant", "declining", "micromanag", "bureaucra", "concern",
        "limited growth", "below market", "underpaid", "competitive market",
        "downsizing", "struggling", "uncertain",
    ]

    # Combine all research text
    research_text = " ".join(filter(None, [
        job.culture_insights, job.hiring_sentiment,
        job.growth_opportunities, job.day_in_life,
    ])).lower()

    if not research_text:
        return 50

    # Count signal matches
    pos_count = sum(1 for s in positive_signals if s in research_text)
    neg_count = sum(1 for s in negative_signals if s in research_text)

    # Adjust score based on signal balance
    score += pos_count * 5   # each positive signal adds 5
    score -= neg_count * 7   # negatives weigh heavier

    # Culture alignment with profile values
    values = _safe_json(profile.values, [])
    if values and job.culture_insights:
        culture_lower = job.culture_insights.lower()
        value_hits = sum(1 for v in values if v.lower() in culture_lower)
        score += value_hits * 6

    # Growth alignment with profile growth areas
    growth_areas = _safe_json(profile.growth_areas, [])
    if growth_areas and job.growth_opportunities:
        growth_lower = job.growth_opportunities.lower()
        growth_hits = sum(1 for g in growth_areas if g.lower() in growth_lower)
        score += growth_hits * 5

    return max(0, min(100, score))


async def score_job_ai(job: Job, profile: Profile, company: Company = None) -> dict:
    """Use AI for intelligent, context-aware scoring.

    Goes beyond keyword matching — considers salary appropriateness for level,
    career trajectory alignment, role nuance, and market context.
    Falls back to rule-based scoring if AI is unavailable.
    """
    if get_provider() == "none":
        return score_job_multidim(job, profile, company)

    # Build rich profile context
    profile_context = []
    profile_context.append(f"Target roles: {profile.target_roles}")
    profile_context.append(f"Skills: {profile.skills}")
    profile_context.append(f"Experience: {profile.experience_years} years")
    profile_context.append(f"Seniority level: {profile.seniority_level or 'not specified'}")
    tiers_up = getattr(profile, 'search_tiers_up', 0) or 0
    tiers_down = getattr(profile, 'search_tiers_down', 0) or 0
    if tiers_up > 0 or tiers_down > 0:
        tier_note = []
        if tiers_up > 0:
            tier_note.append(f"actively seeking roles UP TO {tiers_up} level(s) ABOVE their current tier")
        if tiers_down > 0:
            tier_note.append(f"open to roles {tiers_down} level(s) below")
        profile_context.append(f"Seniority search range: {'; '.join(tier_note)} — roles within this range should score seniority 75-85, NOT be penalized as 'stretch'")
    profile_context.append(f"Location preference: {_safe_json(profile.target_locations, [])}")
    profile_context.append(f"Remote preference: {profile.remote_preference}")
    profile_context.append(f"Salary range: ${f'{profile.min_salary:,}' if profile.min_salary else '?'} - ${f'{profile.max_salary:,}' if profile.max_salary else '?'}" if profile.min_salary else "Salary: not specified")
    if profile.profile_summary:
        profile_context.append(f"Profile summary: {profile.profile_summary[:500]}")
    if profile.career_trajectory:
        profile_context.append(f"Career trajectory: {profile.career_trajectory[:300]}")
    if profile.ideal_culture:
        profile_context.append(f"Ideal culture: {profile.ideal_culture[:300]}")
    if profile.values:
        profile_context.append(f"Values: {profile.values}")
    if profile.deal_breakers:
        profile_context.append(f"Deal breakers: {profile.deal_breakers}")
    if profile.strengths:
        profile_context.append(f"Key strengths: {profile.strengths}")
    if profile.leadership_style:
        profile_context.append(f"Leadership style: {profile.leadership_style[:200]}")

    # Build company context
    company_context = ""
    if company:
        parts = [f"Company: {company.name}"]
        if company.industry:
            parts.append(f"Industry: {company.industry}")
        if company.size:
            parts.append(f"Size: {company.size}")
        if company.glassdoor_rating:
            parts.append(f"Glassdoor: {company.glassdoor_rating}/5 ({company.glassdoor_reviews_count or '?'} reviews)")
        if company.culture_summary:
            parts.append(f"Culture: {company.culture_summary[:200]}")
        if company.score_overall:
            parts.append(f"Employer scorecard overall: {company.score_overall}/100")
        if company.pros:
            parts.append(f"Pros: {company.pros}")
        if company.cons:
            parts.append(f"Cons: {company.cons}")
        company_context = "\n".join(parts)

    prompt = f"""You are a calibrated career match analyst. Score this job match across 6 dimensions.

CRITICAL SCORING RULES:
- Be HONEST and DISCRIMINATING. Most jobs should score 40-70 overall. Reserve 80+ for genuinely strong matches.
- DOMAIN MISMATCH: If the candidate targets IT/cyber security but the job is physical security (guard, patrol, loss prevention, CCTV), score role_fit 0-5. Same for other domain mismatches (e.g., software engineer vs mechanical engineer). This is the most important check.
- Role Fit: Consider career trajectory, not just keyword overlap. A "Senior Analyst" role is NOT a great match for someone targeting "Director of Strategy" even though they're related fields. Score based on how well this advances their career goals.
  * CRITICAL: Role fit should primarily reflect how closely the JOB TITLE matches the TARGET ROLES. If the candidate wants "Director, Information Security" but the job is "Director, Technology Risk" or "Director, Risk Assessment", that's an ADJACENT role, not a match — score role_fit 30-50, not 70+. Only score 70+ when the title clearly aligns with a target role.
  * Skills shared between adjacent fields (e.g., "risk" appears in both security and pure risk roles) should NOT inflate role_fit — that's what the skills dimension is for.
- Skills: Don't just count keyword matches. Consider depth of match - are these core skills or peripheral ones? Does the role require skills at the right level?
- Compensation: DO NOT score 100 just because salary is in range. Consider:
  * Is this salary APPROPRIATE for the seniority level? A $150k role for a VP candidate is underpaid even if their "minimum" is $140k.
  * How does it compare to market rates for this role/location?
  * Is the salary competitive or just adequate?
  * No salary listed = 35 (uncertainty penalty), estimated salary = 40.
- Location: Exact city match = high. Same region = moderate. Remote when they want remote = high. Hybrid when they want remote = moderate at best.
- Seniority: Consider whether this is a step up, lateral, or step down. A lateral move for a senior person is only ~60, not 100. IMPORTANT: If the candidate specifies a "seniority search range" indicating they actively seek higher-level roles (e.g., a Director seeking VP/C-suite), score those in-range higher roles at 75-85 for seniority, NOT as a penalty.
- Culture Fit: Use company data if available. Generic/unknown company culture = 45-55, not higher.

CANDIDATE:
{chr(10).join(profile_context)}

JOB:
Title: {job.title}
Company: {job.company}
Location: {job.location}
Seniority: {job.seniority_level or 'unknown'}
Salary: {job.salary_text or 'not listed'}
{"(salary is estimated, not confirmed)" if job.salary_estimated else ""}
Remote: {job.remote_type or 'unknown'}
Description: {(job.description or '')[:2000]}
{f'Role summary: {job.role_summary}' if job.role_summary else ''}

{f'COMPANY DATA:{chr(10)}{company_context}' if company_context else ''}

{_build_research_context(job)}

Return JSON:
{{
    "breakdown": {{
        "role_fit": <0-100>,
        "skills": <0-100>,
        "location": <0-100>,
        "compensation": <0-100>,
        "seniority": <0-100>,
        "culture_fit": <0-100>{', "research_fit": <0-100>' if job.deep_researched else ''}
    }},
    "reasons": ["top 3-5 SHORT reasons this is or isn't a good match"],
    "concerns": ["1-3 specific concerns or misalignments, if any"],
    "domain_match": true/false  // false if the job is in a completely different professional domain than the candidate targets
}}

CALIBRATION CHECK: Before answering, ask yourself - would a thoughtful career advisor recommend this specific role to this specific candidate? Score accordingly."""

    data = await ai_generate_json(prompt, max_tokens=700, model_tier="fast")
    if data and "breakdown" in data:
        bd = data["breakdown"]
        has_research = bool(job.deep_researched and bd.get("research_fit") is not None)
        active_weights = RESEARCH_WEIGHTS if has_research else BASE_WEIGHTS

        # Clamp all values to 0-100
        for dim in active_weights:
            bd[dim] = max(0, min(100, bd.get(dim, 50)))

        overall = sum(
            bd.get(dim, 50) * (active_weights[dim] / 100)
            for dim in active_weights
        )
        # Propagate domain_match into breakdown
        bd["domain_match"] = data.get("domain_match", True)

        reasons = data.get("reasons", [])
        concerns = data.get("concerns", [])
        if concerns:
            reasons.extend([f"⚠ {c}" for c in concerns[:2]])
        return {
            "score": round(overall, 1),
            "breakdown": bd,
            "reasons": reasons,
            "weights": active_weights,
        }

    # Fallback to rule-based
    return score_job_multidim(job, profile, company)


def score_and_update_job(db, job: Job, profile: Profile, company: Company = None) -> Job:
    """Score a job using rule-based scoring and update the database record."""
    result = score_job_multidim(job, profile, company)
    job.match_score = result["score"]
    job.match_reasons = json.dumps(result["reasons"])
    job.match_breakdown = json.dumps(result["breakdown"])
    db.commit()
    return job


async def score_and_update_job_ai(db, job: Job, profile: Profile, company: Company = None) -> Job:
    """Score a job using AI-powered intelligent scoring and update the database record.
    Falls back to rule-based scoring if AI is unavailable."""
    result = await score_job_ai(job, profile, company)
    job.match_score = result["score"]
    job.match_reasons = json.dumps(result["reasons"])
    job.match_breakdown = json.dumps(result["breakdown"])
    db.commit()
    return job


# Keep for backward compat
def score_job_basic(job: Job, profile: Profile) -> tuple[float, list[str]]:
    """Legacy wrapper - returns (score, reasons) tuple."""
    result = score_job_multidim(job, profile)
    return result["score"], result["reasons"]
