"""Profile-related API routes."""
import asyncio
import json
import os
import re
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel

from backend.database import get_db, SessionLocal
from backend.models.models import Profile, ProfileQuestion, User
from backend.auth import get_optional_user
from backend.services.resume_parser import parse_resume
from backend.services.ai import ai_generate, ai_generate_json, get_provider
from backend.services.enrichment import analyze_profile
from backend.tasks import run_background, find_running_task
from backend.utils import safe_json
from backend.serializers import _profile_dict
from backend.routes._helpers import _get_profile_for_user, UPLOAD_DIR, GCS_BUCKET, _get_gcs_bucket

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Pydantic schemas ──────────────────────────────────────────────────────

class ProfileCreate(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    target_roles: list[str] = []
    target_locations: list[str] = []
    min_salary: Optional[int] = None
    max_salary: Optional[int] = None
    remote_preference: str = "any"
    experience_years: Optional[int] = None
    skills: list[str] = []
    cover_letter_template: Optional[str] = None
    raw_profile_doc: Optional[str] = None
    search_tiers_down: Optional[int] = 0
    search_tiers_up: Optional[int] = 0
    pin: Optional[str] = None
    seniority_level: Optional[str] = None
    availability: Optional[str] = None
    employment_type: Optional[str] = None
    commute_tolerance: Optional[str] = None
    relocation: Optional[str] = None
    company_size: Optional[str] = None
    industry_preference: Optional[str] = None
    top_priority: Optional[str] = None
    security_clearance: Optional[str] = None
    travel_willingness: Optional[str] = None
    additional_notes: Optional[str] = None
    deal_breakers: Optional[str] = None
    ideal_culture: Optional[str] = None
    values: Optional[str] = None
    strengths: Optional[str] = None
    growth_areas: Optional[str] = None

class ProfileUpdate(BaseModel):
    """All fields optional for partial updates (e.g. Reporter Corner saving one field at a time)."""
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    target_roles: Optional[list[str]] = None
    target_locations: Optional[list[str]] = None
    min_salary: Optional[int] = None
    max_salary: Optional[int] = None
    remote_preference: Optional[str] = None
    experience_years: Optional[int] = None
    skills: Optional[list[str]] = None
    cover_letter_template: Optional[str] = None
    raw_profile_doc: Optional[str] = None
    search_tiers_down: Optional[int] = None
    search_tiers_up: Optional[int] = None
    pin: Optional[str] = None
    seniority_level: Optional[str] = None
    availability: Optional[str] = None
    employment_type: Optional[str] = None
    commute_tolerance: Optional[str] = None
    relocation: Optional[str] = None
    company_size: Optional[str] = None
    industry_preference: Optional[str] = None
    top_priority: Optional[str] = None
    security_clearance: Optional[str] = None
    travel_willingness: Optional[str] = None
    additional_notes: Optional[str] = None
    deal_breakers: Optional[str] = None
    ideal_culture: Optional[str] = None
    values: Optional[str] = None
    strengths: Optional[str] = None
    growth_areas: Optional[str] = None
    industry_preferences: Optional[object] = None  # accepts list or JSON string
    career_history: Optional[object] = None  # accepts list or JSON string
    profile_summary: Optional[str] = None
    career_trajectory: Optional[str] = None

class ProfilePasteInput(BaseModel):
    text: str

class AnswerSubmit(BaseModel):
    answer: str


# ── Profile endpoints ─────────────────────────────────────────────────────

@router.get("/profiles")
def list_profiles(db: Session = Depends(get_db), user: User = Depends(get_optional_user)):
    if user:
        profiles = db.query(Profile).filter(
            (Profile.user_id == user.id) | (Profile.user_id.is_(None))
        ).all()
    else:
        profiles = db.query(Profile).all()
    return [_profile_dict(p) for p in profiles]


@router.post("/profiles/select")
def select_profile(data: dict, db: Session = Depends(get_db)):
    """Select a profile by ID, optionally verifying PIN."""
    profile_id = data.get("profile_id")
    pin = data.get("pin", "")

    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    # If profile has a PIN set, verify it
    if profile.pin and profile.pin != pin:
        raise HTTPException(403, "Incorrect PIN")

    return _profile_dict(profile)


@router.post("/profiles/{profile_id}/set-pin")
def set_profile_pin(profile_id: int, data: dict, db: Session = Depends(get_db)):
    """Set or update a PIN for profile protection."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    profile.pin = data.get("pin", "") or None
    db.commit()
    return {"message": "PIN updated"}


@router.post("/profiles")
def create_profile(data: ProfileCreate, db: Session = Depends(get_db), user: User = Depends(get_optional_user)):
    profile = Profile(
        name=data.name,
        pin=data.pin or None,
        email=data.email,
        phone=data.phone,
        location=data.location,
        target_roles=json.dumps(data.target_roles),
        target_locations=json.dumps(data.target_locations),
        min_salary=data.min_salary,
        max_salary=data.max_salary,
        remote_preference=data.remote_preference,
        experience_years=data.experience_years,
        skills=json.dumps(data.skills),
        cover_letter_template=data.cover_letter_template,
        raw_profile_doc=data.raw_profile_doc,
        additional_info=json.dumps({}),
        search_tiers_down=data.search_tiers_down or 0,
        search_tiers_up=data.search_tiers_up or 0,
        user_id=user.id if user else None,
    )
    # B01 fix: if resume text was pasted (raw_profile_doc), also set resume_text
    # so has_resume checks pass downstream
    if data.raw_profile_doc and not profile.resume_text:
        profile.resume_text = data.raw_profile_doc
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return _profile_dict(profile)


@router.get("/profiles/{profile_id}")
def get_profile(profile_id: int, db: Session = Depends(get_db), user: User = Depends(get_optional_user)):
    profile = _get_profile_for_user(profile_id, user, db)
    return _profile_dict(profile)


@router.put("/profiles/{profile_id}")
def update_profile(profile_id: int, data: ProfileUpdate, db: Session = Depends(get_db), user: User = Depends(get_optional_user)):
    profile = _get_profile_for_user(profile_id, user, db)
    # Only update fields that were explicitly provided (partial update support)
    json_fields = {'target_roles', 'target_locations', 'skills', 'career_history', 'industry_preferences'}
    simple_fields = [
        'name', 'email', 'phone', 'location', 'min_salary', 'max_salary',
        'remote_preference', 'experience_years', 'cover_letter_template',
        'raw_profile_doc', 'search_tiers_down', 'search_tiers_up', 'pin',
        'seniority_level', 'availability', 'employment_type', 'commute_tolerance',
        'relocation', 'company_size', 'industry_preference', 'top_priority',
        'security_clearance', 'travel_willingness', 'additional_notes',
        'deal_breakers', 'ideal_culture', 'values', 'strengths', 'growth_areas',
        'profile_summary', 'career_trajectory',
    ]
    for f in simple_fields:
        val = getattr(data, f, None)
        if val is not None:
            setattr(profile, f, val)
    for f in json_fields:
        val = getattr(data, f, None)
        if val is not None:
            setattr(profile, f, json.dumps(val))
    # B01 fix: if raw_profile_doc is set but resume_text is empty, copy it over
    # so has_resume checks pass for text-pasted resumes
    raw_doc = getattr(data, 'raw_profile_doc', None)
    if raw_doc and not profile.resume_text:
        profile.resume_text = raw_doc
    db.commit()
    return _profile_dict(profile)


@router.post("/profiles/parse")
async def parse_profile_text(data: ProfilePasteInput):
    """Parse a pasted profile document into structured profile fields.

    Uses a two-stage AI pipeline:
      Stage 1 — Factual extraction of contact info, skills, career history
      Stage 2 — Smart inference of target roles, summary, trajectory from Stage 1
    """
    text = data.text.strip()
    if not text:
        raise HTTPException(400, "No text provided")

    if get_provider() != "none":
        # ── Stage 1: Factual Extraction ──────────────────────────────────
        stage1_prompt = f"""You are a career data extraction expert. Parse this candidate profile/resume into structured JSON.

EXTRACTION STRATEGY — follow these steps carefully:
1. **name**: Look at the VERY FIRST LINE of the document — resumes almost always start with the candidate's name in large/bold text. If the first line looks like a name (1-4 words, capitalized), use it. Also check for "Name:" labels.
2. **email**: Search the entire document for an email pattern (word@domain.tld). Often near the top, in a header/contact section.
3. **phone**: Search for phone number patterns anywhere in the document. Formats: (555) 123-4567, 555-123-4567, +1 555 123 4567, etc.
4. **location**: Look for city/state patterns near the top of the document SEPARATE from the name. Common formats: "City, ST", "City, State", "City, Province", "City, ST ZIP". Also check for "Location:", "Address:" labels. Extract ONLY the geographic location like "City, ST" or "City, Province" — do NOT include the person's name in this field.
   BAD: "Cameron Chojnacki Milton, ON" — this includes the name. GOOD: "Milton, ON"
5. **target_roles**: Extract from RECENT job titles (last 2-3 positions). These must be real, searchable job titles (e.g. "Director, Information Security" NOT "Director-level security roles"). Include title variations (e.g. both "CISO" and "Chief Information Security Officer"). Generate GRANULAR individual titles from compound roles — e.g. "Director, IT Operations & Cybersecurity" should produce BOTH "Director, IT Operations" AND "Director, Cybersecurity". Also check for any "Objective" or "Target" section. Max 12 titles.
6. **target_locations**: Extract specific geographic locations. Include variations like "Toronto, ON" and "GTA". If remote is mentioned, include "Remote". Check address, summary, and preferences sections.
7. **skills**: Extract from BOTH a dedicated "Skills" section AND from bullet points in job descriptions. Use MARKET-STANDARD skill terms that appear in job postings. GOOD: "Risk Management", "Python", "AWS", "ISO 27001". BAD: "building security programs" (too vague), "team player" (cliché). Each skill 1-4 words. Max 20 skills. Include frameworks, certifications, tools, methodologies.
8. **seniority_level**: Based on ACTUAL career level from most recent roles. Must be exactly one of: entry, mid, senior, director, vp, c-suite
9. **experience_years**: Total years of professional experience (integer). Count from earliest role date to present.
10. **min_salary / max_salary**: Extract as integers if mentioned. If only one number, use it for both.
11. **remote_preference**: Must be exactly one of: remote, hybrid, onsite, any
12. **cover_letter_template**: Extract any instructions about tone, style, or approach for cover letters.
13. **career_history**: Extract ALL work experience entries as a list. For each position extract company name, job title, start date, end date (or "Present"), and a brief description of responsibilities/achievements.

CRITICAL: If you truly cannot find a field, set its value to null — do NOT use strings like "Not found", "Unknown", or "N/A".

Return ONLY valid JSON with confidence scores (0.0-1.0) per field:
{{
    "name": "full name",
    "name_confidence": 0.95,
    "email": "email@example.com",
    "email_confidence": 0.99,
    "phone": "555-123-4567",
    "phone_confidence": 0.9,
    "location": "City, ST",
    "location_confidence": 0.85,
    "target_roles": ["Senior Software Engineer", "Staff Engineer", "Tech Lead"],
    "target_roles_confidence": 0.8,
    "target_locations": ["Toronto, ON", "Remote"],
    "target_locations_confidence": 0.7,
    "min_salary": 165000,
    "max_salary": 200000,
    "remote_preference": "any",
    "experience_years": 15,
    "skills": ["Python", "AWS", "Kubernetes", "System Design"],
    "skills_confidence": 0.85,
    "seniority_level": "senior",
    "cover_letter_template": null,
    "career_history": [
        {{"company": "Acme Corp", "title": "Senior Engineer", "start_date": "2020-01", "end_date": "Present", "description": "Led platform team..."}},
        {{"company": "Beta Inc", "title": "Software Engineer", "start_date": "2017-06", "end_date": "2020-01", "description": "Built microservices..."}}
    ],
    "career_history_confidence": 0.9
}}

DOCUMENT:
{text[:15000]}"""

        stage1 = await ai_generate_json(stage1_prompt, max_tokens=4000, model_tier="deep")

        if stage1:
            stage1 = _sanitize_parsed_fields(stage1)

            # ── Stage 2: Smart Inference ─────────────────────────────────
            stage1_json_str = json.dumps(stage1, indent=2, default=str)
            stage2_prompt = f"""You are a career analyst. Given the factual data extracted from a resume (Stage 1 output below), perform smart inference to fill in higher-level insights.

STAGE 1 EXTRACTED DATA:
{stage1_json_str}

Based on this data, return JSON with the following inferred fields:

1. **target_roles**: Look at the last 2-3 job titles in career_history. Generate searchable title variations that a recruiter would use. For example if the last title was "Director, IT Operations & Cybersecurity", generate ["Director, IT Operations", "Director, Cybersecurity", "Director of IT", "IT Operations Director", "Cybersecurity Director"]. Max 12 titles.
2. **skills**: Review the job descriptions in career_history and merge any additional market-standard skills with the Stage 1 skills list. Deduplicate. Max 25 skills total.
3. **experience_years**: Calculate from the earliest career_history start_date to present (today is {datetime.now().strftime('%Y-%m')}). Return an integer.
4. **seniority_level**: Infer from the most recent job title. Must be exactly one of: entry, mid, senior, director, vp, c-suite
5. **profile_summary**: Write a 3-4 sentence executive summary of who this candidate is, what they bring, and what they are looking for. Write in third person.
6. **career_trajectory**: Write a 2-3 sentence narrative of their career arc — where they have been and where they are heading.
7. **industry_preferences**: Infer a list of industries/sectors they would thrive in based on the companies they have worked at and their skills.

Return ONLY valid JSON:
{{
    "target_roles": ["Title 1", "Title 2"],
    "skills": ["Skill 1", "Skill 2"],
    "experience_years": 15,
    "seniority_level": "director",
    "profile_summary": "...",
    "career_trajectory": "...",
    "industry_preferences": ["Technology", "Finance"]
}}"""

            stage2 = await ai_generate_json(stage2_prompt, max_tokens=2000, model_tier="deep")

            # ── Merge Stage 1 + Stage 2 ──────────────────────────────────
            inferred_fields = []

            if stage2:
                # experience_years: Stage 2 wins (date math is more reliable)
                if stage2.get("experience_years") is not None:
                    stage1["experience_years"] = stage2["experience_years"]
                    inferred_fields.append("experience_years")

                # target_roles: Stage 2 wins only if Stage 1 was empty/null
                if not stage1.get("target_roles") and stage2.get("target_roles"):
                    stage1["target_roles"] = stage2["target_roles"]
                    inferred_fields.append("target_roles")

                # seniority_level: Stage 2 wins only if Stage 1 was empty/null
                if not stage1.get("seniority_level") and stage2.get("seniority_level"):
                    stage1["seniority_level"] = stage2["seniority_level"]
                    inferred_fields.append("seniority_level")

                # skills: merge Stage 1 + Stage 2, deduplicate (case-insensitive)
                s1_skills = stage1.get("skills") or []
                s2_skills = stage2.get("skills") or []
                seen_lower = set()
                merged_skills = []
                for skill in s1_skills + s2_skills:
                    if isinstance(skill, str) and skill.strip().lower() not in seen_lower:
                        seen_lower.add(skill.strip().lower())
                        merged_skills.append(skill.strip())
                stage1["skills"] = merged_skills[:25]
                if s2_skills:
                    inferred_fields.append("skills")

                # Inferred-only fields: Stage 2 always wins
                if stage2.get("profile_summary"):
                    stage1["profile_summary"] = stage2["profile_summary"]
                    inferred_fields.append("profile_summary")
                if stage2.get("career_trajectory"):
                    stage1["career_trajectory"] = stage2["career_trajectory"]
                    inferred_fields.append("career_trajectory")
                if stage2.get("industry_preferences"):
                    stage1["industry_preferences"] = stage2["industry_preferences"]
                    inferred_fields.append("industry_preferences")

            stage1["inferred_fields"] = inferred_fields

            # Fallback: fill gaps with regex extraction
            parsed = stage1
            regex_parsed = _regex_parse_profile(text)
            for key in ["name", "email", "phone", "location"]:
                if not parsed.get(key) and regex_parsed.get(key):
                    parsed[key] = regex_parsed[key]
                    parsed[f"{key}_confidence"] = 0.5  # regex fallback confidence
            for key in ["target_roles", "target_locations", "skills"]:
                if not parsed.get(key) and regex_parsed.get(key):
                    parsed[key] = regex_parsed[key]
                    parsed[f"{key}_confidence"] = 0.4
            for key in ["min_salary", "max_salary", "experience_years"]:
                if parsed.get(key) is None and regex_parsed.get(key) is not None:
                    parsed[key] = regex_parsed[key]
            parsed["raw_profile_doc"] = text
            return parsed

    parsed = _regex_parse_profile(text)
    parsed["raw_profile_doc"] = text
    return parsed


def _sanitize_parsed_fields(parsed: dict) -> dict:
    """Strip 'Not found', 'Unknown', 'N/A', empty strings from parsed AI output.
    Convert them to null so the frontend knows no real value was extracted."""
    _empty_values = {"not found", "unknown", "n/a", "none", "null", ""}
    string_fields = ["name", "email", "phone", "location", "remote_preference",
                     "cover_letter_template", "seniority_level",
                     "profile_summary", "career_trajectory"]
    for key in string_fields:
        val = parsed.get(key)
        if isinstance(val, str) and val.strip().lower() in _empty_values:
            parsed[key] = None
    list_fields = ["target_roles", "target_locations", "skills"]
    for key in list_fields:
        val = parsed.get(key)
        if isinstance(val, list):
            parsed[key] = [v for v in val if isinstance(v, str) and v.strip().lower() not in _empty_values]

    # Fix location: if it starts with the extracted name, strip the name out
    name = parsed.get("name")
    location = parsed.get("location")
    if name and location and isinstance(name, str) and isinstance(location, str):
        if location.strip().lower().startswith(name.strip().lower()):
            cleaned = location[len(name):].strip().lstrip(",").strip()
            parsed["location"] = cleaned if cleaned else None

    return parsed


def _regex_parse_profile(text: str) -> dict:
    """Best-effort regex extraction from profile text."""
    import re

    def clean(s):
        return re.sub(r"\*+", "", s).strip()

    result = {
        "name": "", "email": "", "phone": "", "location": "",
        "target_roles": [], "target_locations": [],
        "min_salary": None, "max_salary": None,
        "remote_preference": "any", "experience_years": None,
        "skills": [], "cover_letter_template": "",
    }

    # Name: try labeled field first, then fall back to first non-empty line
    name_match = re.search(r"(?:Full\s*Name|Name)\s*[:\s]\s*([^\n]+)", text, re.I)
    if name_match:
        result["name"] = clean(name_match.group(1))
    else:
        # First line heuristic: resumes typically start with the person's name
        lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
        if lines:
            first_line = re.sub(r"^[#*_\-]+\s*", "", lines[0]).strip()
            # Looks like a name: 1-4 words, mostly alpha, no common non-name patterns
            words = first_line.split()
            if 1 <= len(words) <= 4 and len(first_line) < 60 and not re.search(r"@|http|resume|curriculum|objective|summary|experience", first_line, re.I):
                if all(re.match(r"[A-Za-z\.\'\-]+$", w) for w in words):
                    result["name"] = first_line

    # Email: search anywhere in document
    email_match = re.search(r"[\w.-]+@[\w.-]+\.\w+", text)
    if email_match:
        result["email"] = email_match.group()

    # Phone: try labeled field first, then any phone-like pattern
    phone_match = re.search(r"(?:Phone|Tel|Cell|Mobile|Contact)\s*[:\s]\s*([\d][\d\-() .+]{6,}[\d])", text, re.I)
    if not phone_match:
        # Standalone phone pattern: +1 (555) 123-4567, 555.123.4567, etc.
        phone_match = re.search(r"(?<!\d)(\+?1?\s*[\(\-]?\d{3}[\)\-.\s]+\d{3}[\-.\s]+\d{4})(?!\d)", text)
    if phone_match:
        result["phone"] = phone_match.group(1).strip()

    # Location: try labeled field first, then "City, ST" / "City, Province" patterns
    loc_match = re.search(r"(?:Address|Location|City)\s*[:\s]\s*([^\n]+)", text, re.I)
    if loc_match:
        result["location"] = clean(loc_match.group(1))
    else:
        # "City, ST" or "City, ST ZIP" pattern (US/CA)
        loc_pattern = re.search(
            r"([A-Z][a-zA-Z\s]+,\s*(?:[A-Z]{2}|Ontario|Quebec|Alberta|British Columbia|Manitoba|Saskatchewan|Nova Scotia)(?:\s+\w\d\w\s?\d\w\d|\s+\d{5})?)",
            text
        )
        if loc_pattern:
            result["location"] = loc_pattern.group(1).strip()

    sal_match = re.search(r"\$\s*([\d,]{4,})\s*[-–]\s*\$?\s*([\d,]{4,})", text)
    if sal_match:
        result["min_salary"] = int(sal_match.group(1).replace(",", ""))
        result["max_salary"] = int(sal_match.group(2).replace(",", ""))

    exp_match = re.search(r"(\d+)\+?\s*years", text, re.I)
    if exp_match:
        result["experience_years"] = int(exp_match.group(1))

    if re.search(r"fully\s+remote.*acceptable|remote.*acceptable", text, re.I):
        result["remote_preference"] = "any"
    elif re.search(r"remote\s+only", text, re.I):
        result["remote_preference"] = "remote"
    elif re.search(r"hybrid.*acceptable", text, re.I):
        result["remote_preference"] = "any"

    roles_section = re.search(
        r"(?:Titles|Target Roles)[^:]*:\s*(.*?)(?=\n\s*(?:Sectors|Geography|Compensation|Job boards|Keywords)|\n\n\n)",
        text, re.I | re.DOTALL
    )
    if roles_section:
        roles = re.findall(r"\d+\.\s*(.+)", roles_section.group(1))
        result["target_roles"] = [clean(r) for r in roles if clean(r)]

    geo_section = re.search(
        r"(?:Geography)\s*[:\s]\s*(.*?)(?=\n\s*(?:Compensation|Salary|Target|Sectors)|\n\n\n)",
        text, re.I | re.DOTALL
    )
    if geo_section:
        locations = []
        lines = geo_section.group(1).strip().split("\n")
        for line in lines:
            line = clean(re.sub(r"^[\*\-\d.]+\s*", "", line))
            if line and len(line) > 2 and len(line) < 100:
                for kw in ["Ontario", "Toronto", "GTA", "Remote", "Canada"]:
                    if kw.lower() in line.lower() and kw not in locations:
                        locations.append(kw)
        if not locations:
            locations = ["Ontario", "Remote Canada"]
        result["target_locations"] = locations

    keywords_section = re.search(
        r"(?:Keywords to search|Key\s*Skills|Skills)\s*[:\s]\s*(.*?)(?=\n\s*(?:Job boards|Filters|Seniority|Notes)|\n\n\n)",
        text, re.I | re.DOTALL
    )
    if keywords_section:
        skills = []
        for line in keywords_section.group(1).strip().split("\n"):
            line = clean(re.sub(r"^[\*\-]+\s*", "", line))
            if line and len(line) > 1 and len(line) < 80:
                skills.append(line)
        result["skills"] = skills[:20]

    cl_section = re.search(
        r"(?:Cover Letter Strategy|Cover Letter)\s*\n(.*?)(?=\n\s*(?:Application Form|Job Search|Notes for)|\Z)",
        text, re.I | re.DOTALL
    )
    if cl_section:
        result["cover_letter_template"] = cl_section.group(1).strip()[:3000]

    return result


@router.post("/profiles/{profile_id}/resume")
async def upload_resume(profile_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    ext = os.path.splitext(file.filename)[1]
    filename = f"resume_{profile_id}{ext}"
    content = await file.read()

    bucket = _get_gcs_bucket()
    if bucket:
        # Production: upload to Google Cloud Storage
        blob = bucket.blob(f"resumes/{filename}")
        blob.upload_from_string(content, content_type=file.content_type)
        filepath = f"gs://{GCS_BUCKET}/resumes/{filename}"
        # Also save locally for parsing
        local_path = os.path.join(UPLOAD_DIR, filename)
        with open(local_path, "wb") as f:
            f.write(content)
        resume_text = parse_resume(local_path)
        os.remove(local_path)  # Clean up temp file
    else:
        # Local dev: save to filesystem
        filepath = os.path.join(UPLOAD_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(content)
        resume_text = parse_resume(filepath)

    profile.resume_path = filepath
    profile.resume_text = resume_text
    db.commit()

    return {"message": "Resume uploaded", "text_length": len(resume_text), "resume_text": resume_text}


@router.post("/profiles/{profile_id}/reparse-resume")
async def reparse_resume(profile_id: int, db: Session = Depends(get_db)):
    """Re-parse the resume file with the current parser and update resume_text.
    Clears career_history cache so it gets regenerated. Runs as background task."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")
    if not profile.resume_path or not os.path.exists(profile.resume_path):
        raise HTTPException(400, "No resume file found to re-parse")

    existing_task = find_running_task("reparse-resume", profile_id)
    if existing_task:
        return {"task_id": existing_task, "status": "running"}

    task_id = run_background("reparse-resume", profile_id, _do_reparse_resume,
                             profile_id, profile.resume_path)
    return {"task_id": task_id, "status": "running"}


async def _do_reparse_resume(profile_id: int, resume_path: str):
    """Background worker for resume re-parsing."""
    # parse_resume is sync, run in executor
    loop = asyncio.get_event_loop()
    resume_text = await loop.run_in_executor(None, parse_resume, resume_path)

    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.id == profile_id).first()
        if profile:
            profile.resume_text = resume_text
            profile.career_history = None  # Clear cache so it regenerates
            db.commit()
    finally:
        db.close()

    return {"message": "Resume re-parsed", "text_length": len(resume_text)}


@router.post("/profiles/{profile_id}/analyze")
async def analyze_profile_endpoint(profile_id: int, db: Session = Depends(get_db)):
    """Trigger deep AI analysis of the profile document."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")
    if not profile.raw_profile_doc and not profile.resume_text:
        raise HTTPException(400, "No profile document or resume to analyze")

    # Reset to re-analyze
    profile.profile_analyzed = False
    db.commit()

    await analyze_profile(db, profile)
    return _profile_dict(profile)


# ── Profile Interview Q&A ───────────────────────────────────────────────

async def _synthesize_profile_from_qa(profile, db):
    """AI-powered profile synthesis: takes all Q&A answers and uses them to
    update profile insights (summary, strengths, values, etc.)."""
    answered_qs = db.query(ProfileQuestion).filter(
        ProfileQuestion.profile_id == profile.id,
        ProfileQuestion.is_answered == True,
    ).all()
    if not answered_qs:
        return

    qa_text = "\n\n".join([f"Q: {q.question}\nA: {q.answer}" for q in answered_qs])

    prompt = f"""You are an expert career profiler. Based on the candidate's interview Q&A answers below,
update their profile insights. You must synthesize the answers into structured profile fields.

CURRENT PROFILE:
Name: {profile.name}
Seniority: {profile.seniority_level or 'Unknown'}
Target Roles: {profile.target_roles}
Experience: {profile.experience_years or '?'} years
{f'Current Summary: {profile.profile_summary}' if profile.profile_summary else ''}
{f'Current Career Trajectory: {profile.career_trajectory}' if profile.career_trajectory else ''}
{f'Current Strengths: {profile.strengths}' if profile.strengths else ''}
{f'Current Values: {profile.values}' if profile.values else ''}
{f'Resume excerpt: {profile.resume_text[:1500]}' if profile.resume_text else ''}

INTERVIEW Q&A:
{qa_text[:4000]}

Based on the Q&A, return UPDATED profile insights as JSON. Only include fields where the Q&A provides
new information or where you'd meaningfully improve the current value. Omit fields that wouldn't change.

{{
    "profile_summary": "3-4 sentence executive summary incorporating Q&A insights",
    "career_trajectory": "2-3 sentence narrative of career arc updated with new context",
    "leadership_style": "1-2 sentences if Q&A reveals leadership approach",
    "strengths": ["top 5-7 differentiators informed by Q&A"],
    "growth_areas": ["areas they want to develop based on Q&A"],
    "values": ["work values revealed by Q&A answers"],
    "deal_breakers": ["things they won't accept, from Q&A"],
    "ideal_culture": "2-3 sentences about ideal work culture from Q&A",
    "industry_preferences": ["industries they prefer based on Q&A"]
}}

CRITICAL: Merge new information with existing profile data. Don't lose existing insights — enhance them."""

    try:
        result = await ai_generate_json(prompt, max_tokens=2000, model_tier="balanced")
        if not isinstance(result, dict):
            return

        # Update profile fields where AI provided new values
        if result.get("profile_summary"):
            profile.profile_summary = result["profile_summary"]
        if result.get("career_trajectory"):
            profile.career_trajectory = result["career_trajectory"]
        if result.get("leadership_style"):
            profile.leadership_style = result["leadership_style"]
        if result.get("ideal_culture"):
            profile.ideal_culture = result["ideal_culture"]

        # JSON list fields
        for field in ("strengths", "growth_areas", "values", "deal_breakers", "industry_preferences"):
            if result.get(field) and isinstance(result[field], list):
                setattr(profile, field, json.dumps(result[field]))

        profile.updated_at = datetime.utcnow()
        db.commit()
        logger.info(f"Profile {profile.id} synthesized from Q&A - updated fields: {list(result.keys())}")
    except Exception as e:
        logger.warning(f"Profile Q&A synthesis failed: {e}")


@router.post("/profiles/{profile_id}/generate-questions")
async def generate_profile_questions(profile_id: int, db: Session = Depends(get_db)):
    """Generate LLM-powered interview questions to flesh out the profile (background task)."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    # Prevent duplicate runs
    existing_task = find_running_task("generate-questions", profile_id)
    if existing_task:
        return {"task_id": existing_task, "status": "already_running"}

    task_id = run_background("generate-questions", profile_id, _generate_questions_worker, profile_id)
    return {"task_id": task_id, "status": "started"}


async def _generate_questions_worker(profile_id: int):
    """Background worker for tiered question generation."""
    from backend.models.models import Job
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.id == profile_id).first()
        if not profile:
            return {"generated": 0, "error": "Profile not found"}

        # Get existing answered questions to avoid repeats
        existing = db.query(ProfileQuestion).filter(
            ProfileQuestion.profile_id == profile_id,
        ).all()
        existing_qs = [q.question for q in existing]
        answered_qs = [q for q in existing if q.is_answered]
        answered_context = "\n".join([f"Q: {q.question}\nA: {q.answer}" for q in answered_qs])
        num_answered = len(answered_qs)

        # Gather job descriptions for application-relevant context
        all_jobs = db.query(Job).filter(Job.profile_id == profile_id).all()
        seniority_dist = {}
        job_desc_samples = []
        for j in all_jobs:
            lvl = (j.seniority_level or "unknown").lower()
            seniority_dist[lvl] = seniority_dist.get(lvl, 0) + 1
            if j.description and len(job_desc_samples) < 5:
                job_desc_samples.append(j.description[:800])

        job_desc_context = "\n---\n".join(job_desc_samples) if job_desc_samples else "No job descriptions available yet."

        # Determine tier based on number of answered questions
        if num_answered < 5:
            tier = "basics"
        elif num_answered < 12:
            tier = "resume_improvement"
        elif num_answered < 20:
            tier = "career_strategy"
        else:
            tier = "deep_insights"

        # Build tier-specific prompt
        tier_instructions = _get_tier_instructions(tier)

        prompt = f"""You are a career coach helping a job seeker build the strongest possible application profile.
Your approach is PRACTICAL and APPLICATION-DRIVEN — every question should directly help them fill out
job applications faster, improve their resume, or strengthen their candidacy.

CURRENT TIER: {tier.upper()} (based on {num_answered} questions already answered)
{tier_instructions}

Generate 5 targeted questions for this tier.

CANDIDATE PROFILE:
Name: {profile.name}
Experience: {profile.experience_years} years
Current Seniority: {profile.seniority_level or 'Not specified'}
Skills: {profile.skills}
Target Roles: {profile.target_roles}
Location: {profile.location}
Salary Range: ${f'{profile.min_salary:,}' if profile.min_salary else '?'} - ${f'{profile.max_salary:,}' if profile.max_salary else '?'}
{f'Resume Text (first 2000 chars): {profile.resume_text[:2000]}' if profile.resume_text else ''}
{f'Profile Summary: {profile.profile_summary}' if profile.profile_summary else ''}
{f'Career Trajectory: {profile.career_trajectory}' if profile.career_trajectory else ''}

JOB MARKET CONTEXT:
- {len(all_jobs)} jobs found, seniority distribution: {json.dumps(seniority_dist)}

SAMPLE JOB DESCRIPTIONS (to understand what applications commonly ask):
{job_desc_context[:3000]}

ALREADY ANSWERED (do NOT repeat similar questions):
{answered_context[:2000] if answered_context else 'None yet.'}

ALREADY ASKED (do NOT repeat):
{chr(10).join(existing_qs[-15:]) if existing_qs else 'None'}

Return ONLY valid JSON array of objects:
[
    {{
        "question": "Your question here",
        "category": "application_basics|resume_improvement|experience|motivation|preferences|culture|leadership|technical|self_assessment",
        "priority": 1-10,
        "purpose": "Brief explanation of WHY this question matters (e.g. 'Common on 80% of applications', 'Helps quantify resume achievement')"
    }}
]

CRITICAL: Every question MUST have a "purpose" field explaining its practical value to the job seeker."""

        try:
            result = await ai_generate_json(prompt, max_tokens=1500, model_tier="balanced")
            logger.info(f"AI question generation result type: {type(result)}, value: {str(result)[:200] if result else 'None'}")
        except Exception as e:
            logger.error(f"AI generate failed: {e}", exc_info=True)
            result = None
        if not result:
            try:
                result = await ai_generate_json(prompt, max_tokens=1500, model_tier="flash")
            except Exception:
                pass
        if not result:
            logger.warning("No result from AI for question generation")
            return {"generated": 0}

        questions = result if isinstance(result, list) else result.get("questions", result.get("data", []))
        created = 0
        for q in questions:
            if isinstance(q, dict) and q.get("question"):
                pq = ProfileQuestion(
                    profile_id=profile_id,
                    question=q["question"],
                    category=q.get("category", "general"),
                    priority=q.get("priority", 5),
                    purpose=q.get("purpose", ""),
                )
                db.add(pq)
                created += 1

        db.commit()
        return {"generated": created, "tier": tier}
    finally:
        db.close()


def _get_tier_instructions(tier: str) -> str:
    """Return tier-specific instructions for question generation."""
    if tier == "basics":
        return """TIER 1 — APPLICATION BASICS & QUICK WINS
Focus on questions that job applications commonly ask and that pre-fill application forms:
- Work authorization / visa status / sponsorship needs
- Availability / earliest start date / notice period
- Willingness to relocate, travel requirements
- Salary expectations (confirm or refine)
- Remote/hybrid/onsite preferences with specifics
- Do they have reliable transportation? (if location-based roles)
- Are they open to contract/temp/part-time or only full-time?
- Any gaps in employment that need a prepared explanation?
- LinkedIn URL, portfolio, GitHub, or professional website

Categories to use: application_basics, preferences
Priority: 8-10 (these are high-priority since they're needed for every application)"""

    elif tier == "resume_improvement":
        return """TIER 2 — RESUME IMPROVEMENT & QUANTIFICATION
Focus on extracting specific details that strengthen the resume:
- Missing metrics: "How many people/projects/budgets did you manage at [Company X]?"
- Quantifiable achievements: "What measurable outcome resulted from [project Y]?"
- Skills gaps: Tools, certifications, or technologies used but not listed
- Achievement framing: Help turn duties into accomplishments
- Notable projects or wins not mentioned in the resume
- Awards, publications, speaking engagements, patents
- Volunteer work or side projects relevant to target roles

Look at their resume text and ask about SPECIFIC bullet points that could be stronger.

Categories to use: resume_improvement, experience, technical
Priority: 7-9"""

    elif tier == "career_strategy":
        return """TIER 3 — CAREER STRATEGY & POSITIONING
Now go deeper into career strategy:
- Why they're leaving / left their current/last role
- What specifically attracted them to their target roles
- How they handle common interview scenarios (conflict, failure, leadership)
- Their management/leadership style and philosophy
- Industry or company size preferences and why
- What makes them uniquely qualified vs other candidates
- Career trajectory narrative — how does it all connect?

Categories to use: motivation, culture, leadership, self_assessment
Priority: 5-8"""

    else:  # deep_insights
        return """TIER 4 — DEEP INSIGHTS & DIFFERENTIATION
Advanced questions for a fully-developed profile:
- Long-term career vision (3-5 years)
- Deal-breakers and non-negotiables they haven't mentioned
- How they want to grow — what skills are they excited to develop?
- Ideal team dynamics and company culture specifics
- What kind of impact do they want to make?
- How do they handle ambiguity, rapid change, or high-pressure situations?
- What would make them turn down an otherwise perfect offer?

Categories to use: culture, self_assessment, motivation, preferences
Priority: 3-6"""


@router.get("/profiles/{profile_id}/interview-questions")
def get_interview_questions(profile_id: int, answered: bool = Query(False), db: Session = Depends(get_db)):
    """Get interview questions for profile building."""
    query = db.query(ProfileQuestion).filter(ProfileQuestion.profile_id == profile_id)
    if not answered:
        query = query.filter(ProfileQuestion.is_answered == False)
    questions = query.order_by(ProfileQuestion.priority.desc(), ProfileQuestion.created_at).all()
    return [{
        "id": q.id,
        "question": q.question,
        "category": q.category,
        "priority": q.priority,
        "purpose": q.purpose or "",
        "answer": q.answer,
        "is_answered": q.is_answered,
    } for q in questions]


@router.post("/profiles/{profile_id}/interview-questions/{question_id}/answer")
async def answer_interview_question(profile_id: int, question_id: int, data: AnswerSubmit, db: Session = Depends(get_db)):
    """Answer a profile interview question."""
    q = db.query(ProfileQuestion).filter(
        ProfileQuestion.id == question_id,
        ProfileQuestion.profile_id == profile_id,
    ).first()
    if not q:
        raise HTTPException(404, "Question not found")

    q.answer = data.answer
    q.is_answered = True
    q.answered_at = datetime.utcnow()
    db.commit()

    # Update profile additional_info with the answer for future use
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if profile:
        additional = safe_json(profile.additional_info, {})
        additional[q.question] = data.answer
        profile.additional_info = json.dumps(additional)
        db.commit()

    remaining = db.query(ProfileQuestion).filter(
        ProfileQuestion.profile_id == profile_id,
        ProfileQuestion.is_answered == False,
    ).count()

    # Every 3 answers, trigger AI profile synthesis in background
    total_answered = db.query(ProfileQuestion).filter(
        ProfileQuestion.profile_id == profile_id,
        ProfileQuestion.is_answered == True,
    ).count()
    profile_updated = False
    if total_answered > 0 and total_answered % 3 == 0 and profile:
        try:
            await _synthesize_profile_from_qa(profile, db)
            profile_updated = True
        except Exception as e:
            logger.warning(f"Profile synthesis after Q&A failed: {e}")

    return {"status": "answered", "remaining": remaining, "profile_updated": profile_updated}


@router.post("/profiles/{profile_id}/questions/{question_id}/ai-draft")
async def ai_draft_answer(profile_id: int, question_id: int, db: Session = Depends(get_db)):
    """Use AI to draft an answer to a profile interview question based on profile and resume."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    question = db.query(ProfileQuestion).filter(
        ProfileQuestion.id == question_id,
        ProfileQuestion.profile_id == profile_id,
    ).first()
    if not question:
        raise HTTPException(404, "Question not found")

    # Build context from profile data
    profile_context = f"Name: {profile.name or 'Unknown'}"
    if profile.location:
        profile_context += f"\nLocation: {profile.location}"
    if profile.target_roles:
        roles = json.loads(profile.target_roles) if isinstance(profile.target_roles, str) else profile.target_roles
        profile_context += f"\nTarget Roles: {', '.join(roles) if isinstance(roles, list) else roles}"
    if profile.skills:
        skills = json.loads(profile.skills) if isinstance(profile.skills, str) else profile.skills
        profile_context += f"\nSkills: {', '.join(skills) if isinstance(skills, list) else skills}"
    if profile.experience_years:
        profile_context += f"\nExperience: {profile.experience_years} years"

    resume_excerpt = ""
    if profile.resume_text:
        resume_excerpt = f"\n\nResume:\n{profile.resume_text[:3000]}"
    elif profile.raw_profile_doc:
        resume_excerpt = f"\n\nProfile Document:\n{profile.raw_profile_doc[:3000]}"

    # Check for previously answered questions for additional context
    answered_qs = db.query(ProfileQuestion).filter(
        ProfileQuestion.profile_id == profile_id,
        ProfileQuestion.is_answered == True,
    ).limit(5).all()
    qa_context = ""
    if answered_qs:
        qa_context = "\n\nPreviously answered questions:\n" + "\n".join(
            f"Q: {q.question}\nA: {q.answer}" for q in answered_qs
        )

    prompt = f"""Based on this candidate's profile and resume, draft a concise answer to this interview question. Be specific, use concrete examples from their experience. Keep it 2-4 sentences.

Candidate Profile:
{profile_context}{resume_excerpt}{qa_context}

Interview Question: {question.question}

Draft a first-person answer as the candidate. Be natural and conversational, not robotic. Return ONLY the answer text, no quotes or labels."""

    try:
        draft = await ai_generate(prompt, max_tokens=500, model_tier="balanced")
        if not draft or not draft.strip():
            raise HTTPException(500, "AI did not generate a draft")
        return {"draft": draft.strip()}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"AI draft failed: {e}")
        raise HTTPException(500, f"AI draft failed: {str(e)}")
