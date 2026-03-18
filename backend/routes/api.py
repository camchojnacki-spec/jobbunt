"""API routes for Jobbunt."""
import asyncio
import json
import os
import re
import logging
import shutil
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File, Form, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel

from backend.database import get_db
from backend.models.models import Job, Application, Profile, AgentQuestion, Company, ProfileQuestion, User
from backend.auth import get_optional_user
from backend.services.scraper import search_all_sources, save_scraped_jobs, fetch_job_details, _get_source_config, _save_source_config, get_source_health, AVAILABLE_SOURCES, SOURCE_CONFIG_PATH
from backend.services.scorer import score_job_basic, score_and_update_job, score_and_update_job_ai, score_job_multidim
from backend.services.agent import start_application, process_application, answer_question, generate_cover_letter, submit_application
from backend.services.resume_parser import parse_resume
from backend.services.ai import ai_generate, ai_generate_json, get_provider
from backend.services.enrichment import enrich_job, enrich_company, get_or_create_company, company_dict, analyze_profile
from backend.services.browser_scraper import build_search_urls, get_extractor, detect_site, DETAIL_EXTRACTOR, SCROLL_AND_LOAD, PAGINATION_URLS
from backend.tasks import run_background, get_task_status, find_running_task
from backend.database import SessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def _safe_json(raw: str | None, default=None):
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

# Progress tracking for long-running tasks
_rescore_progress = {}  # {profile_id: {"current": n, "total": n, "status": "running"|"done"|"error"}}

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


async def _safe_enrich(db: Session, job: Job, profile: Profile):
    """Wrapper for enrich_job that catches exceptions for use with asyncio.gather."""
    try:
        await enrich_job(db, job, profile)
    except Exception as e:
        logger.warning(f"Enrichment failed for job {job.id}: {e}")


async def _safe_score(db: Session, job: Job, profile: Profile):
    """Wrapper for scoring that falls back to rule-based on error."""
    company = db.query(Company).filter(Company.id == job.company_id).first() if job.company_id else None
    try:
        await score_and_update_job_ai(db, job, profile, company)
    except Exception as e:
        logger.warning(f"AI scoring failed for job {job.id}, falling back to rule-based: {e}")
        score_and_update_job(db, job, profile, company)


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

class ProfileUpdate(ProfileCreate):
    pass

class ProfilePasteInput(BaseModel):
    text: str

class SwipeAction(BaseModel):
    action: str  # "like", "pass", or "shortlist"

class AnswerSubmit(BaseModel):
    answer: str


# ── Background Task Status ────────────────────────────────────────────────

@router.get("/tasks/{task_id}")
async def poll_task_status(task_id: str):
    """Poll a background task for its status and result."""
    task = get_task_status(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


# ── Auth helpers ──────────────────────────────────────────────────────────

def _get_profile_for_user(profile_id: int, user, db: Session) -> Profile:
    """Get a profile by ID, checking ownership if user is authenticated."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")
    if user and profile.user_id and profile.user_id != user.id:
        raise HTTPException(403, "Not your profile")
    return profile


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
    profile.name = data.name
    profile.email = data.email
    profile.phone = data.phone
    profile.location = data.location
    profile.target_roles = json.dumps(data.target_roles)
    profile.target_locations = json.dumps(data.target_locations)
    profile.min_salary = data.min_salary
    profile.max_salary = data.max_salary
    profile.remote_preference = data.remote_preference
    profile.experience_years = data.experience_years
    profile.skills = json.dumps(data.skills)
    profile.cover_letter_template = data.cover_letter_template
    if data.raw_profile_doc is not None:
        profile.raw_profile_doc = data.raw_profile_doc
    if data.search_tiers_down is not None:
        profile.search_tiers_down = data.search_tiers_down
    if data.search_tiers_up is not None:
        profile.search_tiers_up = data.search_tiers_up
    if data.pin is not None:
        profile.pin = data.pin or None
    db.commit()
    return _profile_dict(profile)


@router.post("/profiles/parse")
async def parse_profile_text(data: ProfilePasteInput):
    """Parse a pasted profile document into structured profile fields."""
    text = data.text.strip()
    if not text:
        raise HTTPException(400, "No text provided")

    if get_provider() != "none":
        prompt = f"""You are a career data extraction expert. Parse this candidate profile document into structured JSON.

CRITICAL RULES FOR EACH FIELD:
- **target_roles**: Extract EXACT job titles the candidate is targeting. These must be real, searchable job titles
  (e.g. "Director, Information Security" NOT "Director-level security roles"). Include title variations
  (e.g. both "CISO" and "Chief Information Security Officer"). Max 10 titles.
- **target_locations**: Extract specific geographic locations (city, province/state, country).
  Include variations like "Toronto, ON" and "GTA". If remote is mentioned, include "Remote" as a location too.
- **skills**: Extract MARKET-STANDARD skill terms that would appear in job postings.
  GOOD: "Risk Management", "Cloud Security", "ISO 27001", "NIST CSF", "Incident Response", "Python", "AWS"
  BAD: "building security programs" (too vague), "strong communicator" (soft skill phrase), "team player" (cliché)
  Each skill should be 1-4 words, a noun/noun-phrase that a recruiter would search for. Max 20 skills.
  Include frameworks, certifications, tools, methodologies, and domain expertise.
- **seniority_level**: Based on the candidate's ACTUAL career level (not aspirational).
  Must be exactly one of: entry, mid, senior, director, vp, c-suite
- **experience_years**: Total years of professional experience (integer). Count from first relevant role.
- **min_salary / max_salary**: Extract as integers (no currency symbols, no commas). If only one number given, use it for both.
- **remote_preference**: Must be exactly one of: remote, hybrid, onsite, any
- **cover_letter_template**: Extract any instructions about tone, style, or approach for cover letters.

Return ONLY valid JSON:
{{
    "name": "full name",
    "email": "email address",
    "phone": "phone number",
    "location": "city, province/state",
    "target_roles": ["Chief Information Security Officer", "CISO", "VP Information Security"],
    "target_locations": ["Toronto, ON", "GTA", "Ontario", "Remote"],
    "min_salary": 165000,
    "max_salary": 200000,
    "remote_preference": "any",
    "experience_years": 15,
    "skills": ["Risk Management", "Cloud Security", "ISO 27001", "Incident Response"],
    "seniority_level": "director",
    "cover_letter_template": "professional, confident tone..."
}}

DOCUMENT:
{text[:6000]}"""
        parsed = await ai_generate_json(prompt, max_tokens=1500, model_tier="fast")
        if parsed:
            parsed["raw_profile_doc"] = text
            return parsed

    parsed = _regex_parse_profile(text)
    parsed["raw_profile_doc"] = text
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

    name_match = re.search(r"(?:Full\s*Name|Name)\s*[:\s]\s*([^\n]+)", text, re.I)
    if name_match:
        result["name"] = clean(name_match.group(1))

    email_match = re.search(r"[\w.-]+@[\w.-]+\.\w+", text)
    if email_match:
        result["email"] = email_match.group()

    phone_match = re.search(r"(?:Phone|Tel)\s*[:\s]\s*([\d][\d\-() .+]{6,}[\d])", text, re.I)
    if phone_match:
        result["phone"] = phone_match.group(1).strip()

    loc_match = re.search(r"(?:Address|Location|City)\s*[:\s]\s*([^\n]+)", text, re.I)
    if loc_match:
        result["location"] = clean(loc_match.group(1))

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
    filepath = os.path.join(UPLOAD_DIR, filename)

    with open(filepath, "wb") as f:
        content = await file.read()
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


# ── Job endpoints ─────────────────────────────────────────────────────────

@router.get("/sources")
def list_sources():
    """List available job search sources."""
    from backend.services.scraper import AVAILABLE_SOURCES
    return [{"key": k, "name": v["name"], "region": v["region"]} for k, v in AVAILABLE_SOURCES.items()]


@router.get("/sources/health")
def source_health():
    """Get health/reliability stats for each scraper source (for debugging)."""
    from backend.services.scraper import get_source_health
    return get_source_health()


@router.post("/profiles/{profile_id}/search")
async def search_jobs(
    profile_id: int,
    background_tasks: BackgroundTasks,
    sources: Optional[list[str]] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_optional_user),
):
    """Trigger a job search for a profile.

    Scrapes jobs synchronously (fast), then enriches and scores in the background.

    Args:
        sources: Optional list of source keys to search (e.g. ["linkedin", "indeed", "gcjobs"]).
                 If omitted, auto-detects based on target locations.
    """
    profile = _get_profile_for_user(profile_id, user, db)

    # Deep-analyze profile if not done yet (uses rich pasted doc)
    if not profile.profile_analyzed and (profile.raw_profile_doc or profile.resume_text):
        try:
            await analyze_profile(db, profile)
        except Exception as e:
            logger.warning(f"Profile analysis failed: {e}")

    search_result = await search_all_sources(profile, sources=sources)
    raw_jobs = search_result["jobs"]
    relevance_filtered = search_result.get("relevance_filtered", 0)
    new_jobs = save_scraped_jobs(db, profile_id, raw_jobs)

    # Quick rule-based scoring immediately so jobs appear with some score
    # Batch commits every 10 jobs instead of per-job
    for idx, job in enumerate(new_jobs):
        try:
            company = db.query(Company).filter(Company.id == job.company_id).first() if job.company_id else None
            result = score_job_multidim(job, profile, company)
            job.match_score = result["score"]
            job.match_reasons = json.dumps(result["reasons"])
            job.match_breakdown = json.dumps(result["breakdown"])
            if (idx + 1) % 10 == 0:
                db.commit()
        except Exception:
            pass
    db.commit()  # Final commit for remaining jobs

    # Enrich and AI-score in background (won't block the response)
    job_ids = [j.id for j in new_jobs]
    if job_ids:
        background_tasks.add_task(_bg_enrich_and_score, profile_id, job_ids)

    return {
        "total_found": len(raw_jobs) + relevance_filtered,
        "new_jobs": len(new_jobs),
        "duplicates_skipped": len(raw_jobs) - len(new_jobs),
        "relevance_filtered": relevance_filtered,
    }


class BrowserJobImport(BaseModel):
    """Schema for importing jobs scraped from a browser session."""
    title: str
    company: str
    location: str = ""
    description: str = ""
    url: str = ""
    source: str = "browser"
    salary_text: str = ""
    job_type: str = ""
    remote_type: str = ""
    posted_date: str = ""


@router.post("/profiles/{profile_id}/import-browser-jobs")
async def import_browser_jobs(
    profile_id: int,
    jobs: list[BrowserJobImport],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Import jobs scraped from browser sessions (Indeed, Glassdoor, etc).

    Accepts a list of job objects, deduplicates against existing jobs,
    saves new ones, and triggers background enrichment + scoring.
    """
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    if not jobs:
        return {"total_found": 0, "new_jobs": 0, "duplicates_skipped": 0}

    # Convert to raw job dicts for save_scraped_jobs
    raw_jobs = []
    for j in jobs:
        raw = {
            "title": j.title.strip(),
            "company": j.company.strip(),
            "location": j.location.strip(),
            "description": j.description.strip(),
            "url": j.url.strip(),
            "source": j.source or "browser",
            "salary_text": j.salary_text.strip() if j.salary_text else "",
            "job_type": j.job_type.strip() if j.job_type else "",
            "remote_type": j.remote_type.strip() if j.remote_type else "",
            "posted_date": j.posted_date.strip() if j.posted_date else "",
        }
        if raw["title"] and raw["company"]:
            raw_jobs.append(raw)

    new_jobs = save_scraped_jobs(db, profile_id, raw_jobs)

    # Mark browser-imported jobs as having valid URLs (they came from a real browser)
    for job in new_jobs:
        if job.url:
            job.url_valid = True
    db.commit()

    # Quick rule-based scoring immediately — batch commits every 10
    for idx, job in enumerate(new_jobs):
        try:
            company = db.query(Company).filter(Company.id == job.company_id).first() if job.company_id else None
            result = score_job_multidim(job, profile, company)
            job.match_score = result["score"]
            job.match_reasons = json.dumps(result["reasons"])
            job.match_breakdown = json.dumps(result["breakdown"])
            if (idx + 1) % 10 == 0:
                db.commit()
        except Exception:
            pass
    db.commit()

    # Enrich and AI-score in background
    job_ids = [j.id for j in new_jobs]
    if job_ids:
        background_tasks.add_task(_bg_enrich_and_score, profile_id, job_ids)

    logger.info(f"Browser import: {len(raw_jobs)} submitted, {len(new_jobs)} new, {len(raw_jobs) - len(new_jobs)} dupes")

    return {
        "total_found": len(raw_jobs),
        "new_jobs": len(new_jobs),
        "duplicates_skipped": len(raw_jobs) - len(new_jobs),
        "source": "browser",
    }


@router.get("/profiles/{profile_id}/browser-search-config")
async def browser_search_config(
    profile_id: int,
    sites: Optional[list[str]] = Query(None),
    db: Session = Depends(get_db),
):
    """Get search URLs and JS extractors for browser-based scraping.

    Returns URLs to navigate to and extraction scripts to inject for each site.
    The frontend can open these in iframes or the Chrome MCP can navigate to them.
    """
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    target_roles = _safe_json(profile.target_roles, [])
    target_locations = _safe_json(profile.target_locations, [])

    if not target_roles:
        raise HTTPException(400, "Profile needs target roles for browser search")

    # Build search configs for each role+location combo
    configs = []
    requested_sites = sites or ["indeed", "indeed_ca", "glassdoor", "linkedin"]

    for role in target_roles[:3]:  # Top 3 roles
        for loc in target_locations[:2]:  # Top 2 locations
            urls = build_search_urls(role, loc, requested_sites)
            for site_key, url in urls.items():
                # Map indeed_ca to indeed extractor
                extractor_key = "indeed" if site_key == "indeed_ca" else site_key
                configs.append({
                    "site": site_key,
                    "query": role,
                    "location": loc,
                    "url": url,
                    "extractor": get_extractor(extractor_key),
                    "scroll_script": SCROLL_AND_LOAD,
                    "pagination_script": PAGINATION_URLS,
                })

    return {
        "configs": configs,
        "detail_extractor": DETAIL_EXTRACTOR,
        "total_urls": len(configs),
    }


@router.post("/profiles/{profile_id}/reconcile-duplicates")
async def reconcile_duplicates(profile_id: int, db: Session = Depends(get_db)):
    """AI-driven duplicate reconciliation. Finds potential duplicate jobs
    that slipped past fingerprint + fuzzy matching and merges them.

    Uses AI to compare jobs with similar titles/companies and determine
    if they're truly the same posting from different sources.
    """
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    # Get all pending/liked jobs
    jobs = db.query(Job).filter(
        Job.profile_id == profile_id,
        Job.status.in_(["pending", "liked", "shortlisted"]),
    ).order_by(Job.company, Job.title).all()

    if len(jobs) < 2:
        return {"merged": 0, "total_checked": 0}

    # Group by normalized company for efficient comparison
    import re as _re
    company_groups = {}
    for j in jobs:
        comp_key = _re.sub(r"[^a-z0-9]", "", (j.company or "").lower())[:20]
        company_groups.setdefault(comp_key, []).append(j)

    # Find potential duplicate pairs (same/similar company, different titles)
    candidates = []
    for comp_key, group in company_groups.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for k in range(i + 1, len(group)):
                j1, j2 = group[i], group[k]
                # Skip if already from same source (likely intentionally different roles)
                if j1.source == j2.source and j1.fingerprint != j2.fingerprint:
                    continue
                candidates.append((j1, j2))

    if not candidates:
        return {"merged": 0, "total_checked": 0, "message": "No potential duplicates found"}

    # Batch AI comparison (max 20 pairs at a time to manage tokens)
    candidates = candidates[:20]

    pairs_text = []
    for idx, (j1, j2) in enumerate(candidates):
        pairs_text.append(
            f"Pair {idx}: "
            f"A=[{j1.title} at {j1.company}, {j1.location}, source={j1.source}] vs "
            f"B=[{j2.title} at {j2.company}, {j2.location}, source={j2.source}]"
        )

    prompt = f"""You are a job listing deduplication expert. Determine which pairs are duplicates
(the SAME job posting from different sources or with slightly different titles).

Consider a DUPLICATE if:
- Same company (even with different name formats like "RBC" vs "Royal Bank of Canada")
- Same or very similar role (e.g., "Sr Developer" vs "Senior Software Developer")
- Similar location (same city counts)

NOT a duplicate if:
- Different seniority levels (Junior vs Senior)
- Genuinely different roles at the same company
- Different departments/teams (if distinguishable)

Pairs to check:
{chr(10).join(pairs_text)}

Return a JSON array of pair indices that ARE duplicates. Example: [0, 3, 7]
If none are duplicates, return: []"""

    try:
        result = await ai_generate_json(prompt, max_tokens=200, model_tier="fast")
    except Exception as e:
        logger.warning(f"AI dedup failed: {e}")
        return {"merged": 0, "total_checked": len(candidates), "error": str(e)}

    merged = 0
    if isinstance(result, list):
        for idx in result:
            if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
                continue
            j1, j2 = candidates[idx]
            # Merge: keep the one with more data, add source from the other
            keep, remove = (j1, j2) if len(j1.description or "") >= len(j2.description or "") else (j2, j1)

            # Merge sources
            try:
                keep_sources = json.loads(keep.sources_seen or "[]")
            except (json.JSONDecodeError, TypeError):
                keep_sources = [keep.source] if keep.source else []
            try:
                remove_sources = json.loads(remove.sources_seen or "[]")
            except (json.JSONDecodeError, TypeError):
                remove_sources = [remove.source] if remove.source else []
            for s in remove_sources:
                if s not in keep_sources:
                    keep_sources.append(s)
            keep.sources_seen = json.dumps(keep_sources)

            # Keep better data
            if not keep.url and remove.url:
                keep.url = remove.url
            if remove.description and len(remove.description) > len(keep.description or ""):
                keep.description = remove.description
            if not keep.salary_text and remove.salary_text:
                keep.salary_text = remove.salary_text

            # Mark the duplicate as passed (don't delete, just hide)
            remove.status = "duplicate"
            keep.last_seen = datetime.utcnow()
            merged += 1
            logger.info(f"AI dedup: merged '{remove.title}' ({remove.source}) into '{keep.title}' ({keep.source})")

    db.commit()
    return {"merged": merged, "total_checked": len(candidates)}


async def _bg_enrich_and_score(profile_id: int, job_ids: list[int]):
    """Background task: enrich and AI-score jobs after initial search."""
    from backend.database import SessionLocal
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.id == profile_id).first()
        if not profile:
            return

        jobs = db.query(Job).filter(Job.id.in_(job_ids)).all()
        BATCH_SIZE = 15

        # Enrich in parallel batches
        for i in range(0, len(jobs), BATCH_SIZE):
            batch = jobs[i:i + BATCH_SIZE]
            await asyncio.gather(
                *[_safe_enrich(db, job, profile) for job in batch],
                return_exceptions=True,
            )

        # AI-score in parallel batches
        for i in range(0, len(jobs), BATCH_SIZE):
            batch = jobs[i:i + BATCH_SIZE]
            await asyncio.gather(
                *[_safe_score(db, job, profile) for job in batch],
                return_exceptions=True,
            )

        # Auto-pass jobs that scored below 30 — keeps the swipe queue clean
        auto_passed = 0
        for job in jobs:
            if job.match_score is not None and job.match_score < 30 and job.status == "pending":
                job.status = "passed"
                job.swiped_at = datetime.utcnow()
                auto_passed += 1
        if auto_passed > 0:
            db.commit()
            logger.info(f"Auto-passed {auto_passed} jobs scoring below 30")

        logger.info(f"Background enrich+score complete for {len(jobs)} jobs")
    except Exception as e:
        logger.error(f"Background enrich+score failed: {e}")
    finally:
        db.close()


@router.get("/profiles/{profile_id}/jobs")
def list_jobs(
    profile_id: int,
    status: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    min_score: float = Query(0),
    db: Session = Depends(get_db),
    user: User = Depends(get_optional_user),
):
    """List jobs for a profile, optionally filtered by status and source."""
    _get_profile_for_user(profile_id, user, db)  # ownership check
    query = db.query(Job).filter(Job.profile_id == profile_id)
    if status:
        query = query.filter(Job.status == status)
    else:
        # Exclude duplicates from general listing by default
        query = query.filter(Job.status != "duplicate")
    if source:
        # Filter by source — check both primary source and sources_seen JSON array
        query = query.filter(
            (Job.source == source) | (Job.sources_seen.contains(f'"{source}"'))
        )
    if min_score > 0:
        query = query.filter(Job.match_score >= min_score)
    jobs = query.order_by(Job.match_score.desc()).all()
    return [_job_dict(j, db) for j in jobs]


@router.get("/profiles/{profile_id}/swipe")
def get_swipe_stack(
    profile_id: int,
    limit: int = Query(20),
    db: Session = Depends(get_db),
    user: User = Depends(get_optional_user),
):
    """Get the next batch of jobs to swipe on, ordered by match score.
    Only returns jobs that are confirmed active and meet completeness threshold."""
    _get_profile_for_user(profile_id, user, db)  # ownership check
    jobs = (
        db.query(Job)
        .filter(Job.profile_id == profile_id, Job.status == "pending")
        .order_by(Job.match_score.desc())
        .limit(limit * 3)  # Fetch extra to filter
        .all()
    )
    # Filter: must be complete enough AND confirmed still active
    ready_jobs = []
    for j in jobs:
        if _job_completeness(j) < 40:
            continue
        # url_valid: True = confirmed active, None = unchecked (allow if enriched),
        # False = confirmed dead (keep in results but show as closed)
        # If URL hasn't been checked yet and job has a URL, allow it
        # (browser-imported jobs have verified URLs, enrichment will catch dead links later)
        ready_jobs.append(j)

    return [_job_dict(j, db) for j in ready_jobs[:limit]]


@router.post("/jobs/{job_id}/swipe")
async def swipe_job(job_id: int, data: SwipeAction, db: Session = Depends(get_db)):
    """Swipe on a job - like or pass."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")

    job.swiped_at = datetime.utcnow()

    if data.action == "shortlist":
        job.status = "shortlisted"
        db.commit()
        return {"status": "shortlisted"}
    elif data.action == "like":
        job.status = "liked"
        db.commit()

        profile = db.query(Profile).filter(Profile.id == job.profile_id).first()
        application = await start_application(db, job, profile)

        # If verification failed during start_application, return early
        if application.status == "failed":
            return {
                "status": "failed",
                "application": _application_dict(application),
                "agent_result": {"status": "failed", "error": application.error_message},
            }

        result = await process_application(db, application)

        return {
            "status": "liked",
            "application": _application_dict(application),
            "agent_result": result,
        }
    else:
        job.status = "passed"
        db.commit()
        return {"status": "passed"}


@router.get("/jobs/{job_id}")
async def get_job_detail(job_id: int, db: Session = Depends(get_db)):
    """Get full job details with enrichment data."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")

    # Enrich on demand if not done
    if not job.enriched:
        profile = db.query(Profile).filter(Profile.id == job.profile_id).first()
        try:
            await enrich_job(db, job, profile)
        except Exception as e:
            logger.warning(f"Enrichment failed for job {job.id}: {e}")

    return _job_dict(job, db)


@router.post("/jobs/{job_id}/enrich")
async def enrich_job_endpoint(job_id: int, db: Session = Depends(get_db)):
    """Manually trigger enrichment for a job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    profile = db.query(Profile).filter(Profile.id == job.profile_id).first()
    job.enriched = False
    await enrich_job(db, job, profile)
    return _job_dict(job, db)


@router.post("/jobs/{job_id}/verify")
async def verify_job_endpoint(job_id: int, db: Session = Depends(get_db)):
    """Deep-verify a job posting is still active by checking the page content."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.url:
        return {"verified": False, "reason": "No URL available"}

    from backend.services.enrichment import verify_job_active
    result = await verify_job_active(db, job)
    return result


@router.post("/profiles/{profile_id}/verify-pending")
async def verify_pending_jobs(profile_id: int, db: Session = Depends(get_db)):
    """Verify all unverified pending jobs for a profile. Returns count of verified/expired."""
    from backend.services.enrichment import verify_job_active
    import asyncio

    jobs = (
        db.query(Job)
        .filter(
            Job.profile_id == profile_id,
            Job.status == "pending",
            Job.url_valid.is_(None),
            Job.url.isnot(None),
            Job.url != "",
        )
        .limit(20)  # Batch of 20 at a time
        .all()
    )

    results = {"total": len(jobs), "active": 0, "expired": 0, "errors": 0}
    for job in jobs:
        try:
            result = await verify_job_active(db, job)
            if result.get("verified"):
                results["active"] += 1
            else:
                results["expired"] += 1
        except Exception as e:
            logger.warning(f"Verify failed for job {job.id}: {e}")
            results["errors"] += 1

    return results


@router.post("/profiles/{profile_id}/rescore")
async def rescore_all_jobs(profile_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Re-score all jobs for a profile with the latest scoring engine.
    Runs in background so the server stays responsive for progress polling."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    total = db.query(Job).filter(Job.profile_id == profile_id).count()

    # If already running, don't start another
    existing = _rescore_progress.get(profile_id, {})
    if existing.get("status") == "running":
        return {"message": "Rescore already in progress", "total": existing["total"]}

    _rescore_progress[profile_id] = {"current": 0, "total": total, "status": "running"}
    background_tasks.add_task(_bg_rescore, profile_id)
    return {"message": "Rescore started", "total": total}


async def _bg_rescore(profile_id: int):
    """Background rescore task — uses its own DB session."""
    from backend.database import SessionLocal
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.id == profile_id).first()
        if not profile:
            _rescore_progress[profile_id] = {"current": 0, "total": 0, "status": "error"}
            return

        jobs = db.query(Job).filter(Job.profile_id == profile_id).all()
        total = len(jobs)
        scored = 0

        for job in jobs:
            company = db.query(Company).filter(Company.id == job.company_id).first() if job.company_id else None
            try:
                await score_and_update_job_ai(db, job, profile, company)
            except Exception:
                try:
                    score_and_update_job(db, job, profile, company)
                except Exception:
                    pass
            scored += 1
            _rescore_progress[profile_id] = {"current": scored, "total": total, "status": "running"}

        _rescore_progress[profile_id] = {"current": scored, "total": total, "status": "done"}
    except Exception as e:
        logger.error(f"Background rescore failed: {e}", exc_info=True)
        _rescore_progress[profile_id] = {"current": 0, "total": 0, "status": "error"}
    finally:
        db.close()


@router.get("/profiles/{profile_id}/rescore-progress")
def get_rescore_progress(profile_id: int):
    """Get progress of an ongoing rescore operation."""
    progress = _rescore_progress.get(profile_id, {"current": 0, "total": 0, "status": "idle"})
    return progress


# ── Company endpoints ────────────────────────────────────────────────────

@router.post("/profiles/{profile_id}/reenrich-companies")
async def reenrich_companies(profile_id: int, db: Session = Depends(get_db)):
    """Re-enrich all companies missing website domains (for logo support)."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    # Find all companies linked to this profile's jobs that are missing website
    job_company_ids = (
        db.query(Job.company_id)
        .filter(Job.profile_id == profile_id, Job.company_id.isnot(None))
        .distinct()
        .all()
    )
    company_ids = [cid[0] for cid in job_company_ids]

    companies = db.query(Company).filter(
        Company.id.in_(company_ids),
        (Company.website.is_(None)) | (Company.website == ""),
    ).all()

    updated = 0
    for company in companies:
        company.enriched = False  # Reset to force re-enrichment
        try:
            await enrich_company(db, company)
            if company.website:
                updated += 1
        except Exception as e:
            logger.warning(f"Re-enrich failed for {company.name}: {e}")

    return {"total_missing": len(companies), "updated": updated}


@router.get("/companies/{company_id}")
async def get_company(company_id: int, db: Session = Depends(get_db)):
    """Get company details with enrichment data."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not company.enriched:
        await enrich_company(db, company)
    return company_dict(company)


# ── Application endpoints ────────────────────────────────────────────────

@router.get("/profiles/{profile_id}/applications")
def list_applications(profile_id: int, db: Session = Depends(get_db)):
    apps = db.query(Application).filter(
        Application.profile_id == profile_id,
        Application.status.notin_(["hidden", "cancelled"]),
    ).all()
    return [_application_dict(a) for a in apps]


@router.get("/applications/{app_id}")
def get_application(app_id: int, db: Session = Depends(get_db)):
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(404, "Application not found")
    return _application_dict(app)


@router.get("/applications/{app_id}/questions")
def get_questions(app_id: int, db: Session = Depends(get_db)):
    questions = db.query(AgentQuestion).filter(
        AgentQuestion.application_id == app_id,
        AgentQuestion.is_answered == False,
    ).all()
    return [_question_dict(q) for q in questions]


@router.get("/applications/{app_id}/automation-plan")
async def get_automation_plan(app_id: int, db: Session = Depends(get_db)):
    """Get the automation plan for submitting an application."""
    application = db.query(Application).filter(Application.id == app_id).first()
    if not application:
        raise HTTPException(404, "Application not found")
    from backend.services.automator import build_automation_plan
    plan = await build_automation_plan(db, application)
    return plan


@router.post("/applications/{app_id}/return-to-browse")
async def return_to_browse(app_id: int, db: Session = Depends(get_db)):
    """Return an application back to the browse/swipe phase."""
    application = db.query(Application).filter(Application.id == app_id).first()
    if not application:
        raise HTTPException(404, "Application not found")
    job = application.job
    if job:
        job.status = "pending"
    application.status = "cancelled"
    db.commit()
    return {"status": "returned", "job_id": job.id if job else None}


@router.post("/applications/{app_id}/hide")
async def hide_application(app_id: int, db: Session = Depends(get_db)):
    """Hide an application from the main list."""
    application = db.query(Application).filter(Application.id == app_id).first()
    if not application:
        raise HTTPException(404, "Application not found")
    application.status = "hidden"
    db.commit()
    return {"status": "hidden"}


@router.post("/applications/{app_id}/unhide")
async def unhide_application(app_id: int, db: Session = Depends(get_db)):
    """Unhide an application."""
    application = db.query(Application).filter(Application.id == app_id).first()
    if not application:
        raise HTTPException(404, "Application not found")
    application.status = "ready"
    db.commit()
    return {"status": "unhidden"}


@router.get("/profiles/{profile_id}/applications/hidden")
def list_hidden_applications(profile_id: int, db: Session = Depends(get_db)):
    """List hidden applications."""
    apps = db.query(Application).filter(
        Application.profile_id == profile_id,
        Application.status == "hidden",
    ).all()
    return [_application_dict(a) for a in apps]


@router.post("/applications/{app_id}/submit")
async def mark_submitted(app_id: int, db: Session = Depends(get_db)):
    """Mark an application as submitted (after browser-based submission)."""
    result = await submit_application(db, app_id)
    return result


@router.post("/questions/{question_id}/answer")
async def submit_answer(question_id: int, data: AnswerSubmit, db: Session = Depends(get_db)):
    result = await answer_question(db, question_id, data.answer)
    return result


# ── Dedup & Cleanup ──────────────────────────────────────────────────────

@router.post("/profiles/{profile_id}/dedup-jobs")
def dedup_jobs(profile_id: int, db: Session = Depends(get_db)):
    """Find and remove near-duplicate jobs (same company + similar title, different locations)."""
    import re

    jobs = db.query(Job).filter(
        Job.profile_id == profile_id,
        Job.status == "pending",
    ).order_by(Job.match_score.desc()).all()

    def normalize_title(title):
        t = title.lower().strip()
        t = re.sub(r'[^a-z0-9\s]', '', t)
        t = re.sub(r'\s+', ' ', t)
        return t

    seen = {}  # (company_normalized, title_normalized) -> best_job
    removed = 0
    merged_sources = 0

    for job in jobs:
        company_norm = (job.company or "").lower().strip()
        title_norm = normalize_title(job.title)
        key = (company_norm, title_norm)

        if key in seen:
            best_job = seen[key]
            # Merge sources
            best_sources = json.loads(best_job.sources_seen or "[]")
            job_sources = json.loads(job.sources_seen or "[]")
            for s in job_sources:
                if s not in best_sources:
                    best_sources.append(s)
                    merged_sources += 1
            best_job.sources_seen = json.dumps(best_sources)
            # Keep the best (higher score, already first due to ordering)
            job.status = "passed"  # Mark dupe as passed
            removed += 1
        else:
            seen[key] = job

    db.commit()
    return {"removed": removed, "merged_sources": merged_sources}


@router.post("/profiles/{profile_id}/reset-jobs")
def reset_jobs(profile_id: int, db: Session = Depends(get_db)):
    """Clear all jobs, applications, and companies for a profile. Profile is kept."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    # Delete in order to respect foreign keys
    apps_deleted = db.query(Application).filter(Application.profile_id == profile_id).delete()
    # Delete agent questions linked to applications for this profile
    app_ids = [a.id for a in db.query(Application.id).filter(Application.profile_id == profile_id).all()]
    if app_ids:
        db.query(AgentQuestion).filter(AgentQuestion.application_id.in_(app_ids)).delete(synchronize_session=False)

    jobs_deleted = db.query(Job).filter(Job.profile_id == profile_id).delete()

    db.commit()
    return {"jobs_deleted": jobs_deleted, "applications_deleted": apps_deleted}


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
        additional = _safe_json(profile.additional_info, {})
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


# ── Career Stats (Baseball Card) ─────────────────────────────────────────

@router.post("/profiles/{profile_id}/career-stats")
async def extract_career_stats(profile_id: int, db: Session = Depends(get_db)):
    """Use AI to extract structured work history from resume for baseball card stat lines."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    # Return cached if available
    existing = _safe_json(profile.career_history, [])
    if existing:
        return {"career_history": existing, "cached": True}

    resume = profile.resume_text or profile.raw_profile_doc or ""
    if not resume:
        return {"career_history": [], "cached": False}

    # Check for already-running task
    existing_task = find_running_task("career-stats", profile_id)
    if existing_task:
        return {"task_id": existing_task, "status": "running"}

    # Launch background task
    task_id = run_background("career-stats", profile_id, _do_career_stats, profile_id, resume)
    return {"task_id": task_id, "status": "running"}


async def _do_career_stats(profile_id: int, resume: str):
    """Background worker for career stats extraction."""
    prompt = f"""Extract ALL work history positions from this resume. Look through the ENTIRE document for every job listed under "Professional Experience" or similar sections.

CRITICAL RULES:
- Extract EXACTLY what's in the resume. Do NOT guess, expand abbreviations, or infer company names. Use the exact text from the resume.
- If a company name is abbreviated (e.g., "CBI"), keep it as-is. Do NOT expand to a full name unless it's explicitly stated in the resume.
- Copy company names character-for-character as they appear. Do NOT substitute with what you think the company might be.

Return a JSON array of ALL positions found, ordered most recent first. Each entry:
- "company": company name EXACTLY as written in the resume — copy verbatim, do not rephrase or expand
- "role": job title abbreviated to fit 35 chars (e.g. "Dir. IT Ops & Cybersecurity")
- "start_year": start year as integer. Use null if unknown.
- "end_year": end year as integer. Use null if current/present.
- "years": duration in years as integer
- "highlight": one key achievement (max 35 chars, e.g. "Led 45+ staff across 6 teams", "Built security program from zero")

IMPORTANT: Include EVERY position listed. Do not stop early. Typical resumes have 3-6 positions.

Return ONLY a JSON array, no other text.

Full Resume:
{resume}"""

    result = await ai_generate_json(prompt, max_tokens=1000, model_tier="balanced")
    if not isinstance(result, list):
        result = []

    # Clean up and validate
    career = []
    for entry in result[:8]:
        if not isinstance(entry, dict):
            continue
        career.append({
            "company": str(entry.get("company", "Unknown"))[:30],
            "role": str(entry.get("role", "Unknown"))[:35],
            "start_year": entry.get("start_year"),
            "end_year": entry.get("end_year"),
            "years": entry.get("years", 1),
            "highlight": str(entry.get("highlight", ""))[:40],
        })

    # Cache it in DB with a fresh session
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.id == profile_id).first()
        if profile:
            profile.career_history = json.dumps(career)
            db.commit()
    finally:
        db.close()

    return {"career_history": career, "cached": False}


@router.post("/profiles/{profile_id}/career-stats/refresh")
async def refresh_career_stats(profile_id: int, db: Session = Depends(get_db)):
    """Force re-extract career stats from resume."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")
    profile.career_history = None
    db.commit()
    # Now call the main endpoint which will start a background task
    return await extract_career_stats(profile_id, db)


@router.post("/profiles/{profile_id}/scouting-report")
async def generate_scouting_report(profile_id: int, db: Session = Depends(get_db)):
    """Generate a baseball-themed scouting report narrative for the profile."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    # Check for already-running task
    existing_task = find_running_task("scouting-report", profile_id)
    if existing_task:
        return {"task_id": existing_task, "status": "running"}

    # Gather data needed for the prompt before launching background
    resume = profile.resume_text or profile.raw_profile_doc or ""
    roles = _safe_json_list(profile.target_roles)
    skills = _safe_json_list(profile.skills)
    career = _safe_json(profile.career_history, [])
    name = profile.name
    experience_years = profile.experience_years

    task_id = run_background("scouting-report", profile_id, _do_scouting_report,
                             name, experience_years, roles, skills, career, resume)
    return {"task_id": task_id, "status": "running"}


async def _do_scouting_report(name, experience_years, roles, skills, career, resume):
    """Background worker for scouting report generation."""
    career_summary = ""
    for c in career[:5]:
        career_summary += f"- {c.get('role', '?')} at {c.get('company', '?')} ({c.get('years', '?')} yrs)\n"

    prompt = f"""Write a baseball-scout-style scouting report (4-6 sentences) on this job candidate. Use baseball metaphors: "tools" (skills), "arm" (leadership), "bat" (technical chops), "speed" (adaptability), "baseball IQ" (strategy). Reference real career achievements as "highlight reel plays."

{name} | {experience_years or '?'} yrs | Targeting: {', '.join(roles[:3])}
Skills: {', '.join(skills[:8])}
Career: {career_summary}
Resume: {resume[:1500]}

Return ONLY the scouting report paragraph. No labels or headers."""

    report = await ai_generate(prompt, max_tokens=400, model_tier="balanced")
    return {"scouting_report": report.strip() if report else ""}


# ── Deep Research ────────────────────────────────────────────────────────

@router.post("/jobs/{job_id}/deep-research")
async def deep_research_job(job_id: int, db: Session = Depends(get_db)):
    """Phase 2: Deep research a shortlisted job for culture, interview process, growth, etc."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")

    if job.deep_researched:
        return _job_dict(job, db)

    profile = db.query(Profile).filter(Profile.id == job.profile_id).first()
    company = db.query(Company).filter(Company.id == job.company_id).first() if job.company_id else None

    from backend.services.enrichment import deep_research_job as _deep_research
    await _deep_research(db, job, profile, company)
    return _job_dict(job, db)


@router.post("/profiles/{profile_id}/deep-research-shortlist")
async def deep_research_shortlist(profile_id: int, db: Session = Depends(get_db)):
    """Deep research all liked/shortlisted jobs that haven't been researched yet."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    from backend.services.enrichment import deep_research_job as _deep_research

    # Research liked/shortlisted jobs and high-scoring pending jobs
    jobs = db.query(Job).filter(
        Job.profile_id == profile_id,
        Job.deep_researched == False,
        (Job.status == "liked") | (Job.status == "shortlisted") | (Job.match_score >= 65),
    ).order_by(Job.match_score.desc()).limit(10).all()

    researched = 0
    for job in jobs:
        company = db.query(Company).filter(Company.id == job.company_id).first() if job.company_id else None
        try:
            await _deep_research(db, job, profile, company)
            researched += 1
        except Exception as e:
            logger.warning(f"Deep research failed for job {job.id}: {e}")

    return {"researched": researched, "total": len(jobs)}


# ── Shortlist ─────────────────────────────────────────────────────────

@router.get("/profiles/{profile_id}/shortlist")
def get_shortlist(profile_id: int, db: Session = Depends(get_db)):
    """Get all shortlisted jobs for a profile."""
    jobs = (
        db.query(Job)
        .filter(Job.profile_id == profile_id, Job.status == "shortlisted")
        .order_by(Job.match_score.desc())
        .all()
    )
    return [_job_dict(j, db) for j in jobs]


@router.post("/jobs/{job_id}/unshortlist")
def unshortlist_job(job_id: int, db: Session = Depends(get_db)):
    """Remove a job from the shortlist (back to pending)."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    job.status = "pending"
    job.swiped_at = None
    db.commit()
    return {"status": "pending"}


# ── Search Advisor ────────────────────────────────────────────────────

@router.get("/profiles/{profile_id}/search-advisor")
async def get_search_advisor(profile_id: int, db: Session = Depends(get_db)):
    """AI-powered career coach & search advisor - analyzes career trajectory,
    assesses target realism, and suggests profile adjustments."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    if get_provider() == "none":
        return {"advisor": None, "reason": "No AI provider configured"}

    # Check for already-running task
    existing_task = find_running_task("search-advisor", profile_id)
    if existing_task:
        return {"task_id": existing_task, "status": "running"}

    # Quick pre-check — if there's no data to analyze, return immediately
    target_roles = _safe_json(profile.target_roles, [])
    profile_skills = _safe_json(profile.skills, [])
    if not target_roles and not profile_skills and not profile.resume_text:
        return {"advisor": None, "reason": "Add target roles, skills, or upload a resume before requesting analysis"}

    task_id = run_background("search-advisor", profile_id, _do_search_advisor, profile_id)
    return {"task_id": task_id, "status": "running"}


async def _do_search_advisor(profile_id: int):
    """Background worker for search advisor generation."""
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.id == profile_id).first()
        if not profile:
            return {"advisor": None, "reason": "Profile not found"}
        return await _build_search_advisor(profile_id, profile, db)
    except Exception as e:
        logger.error(f"Search advisor crashed: {e}", exc_info=True)
        return {"advisor": None, "reason": f"Analysis error: {type(e).__name__}: {str(e)[:200]}"}
    finally:
        db.close()


async def _build_search_advisor(profile_id: int, profile, db: Session):
    """Inner logic for search advisor, wrapped so caller can catch all errors."""
    # ── Sanity checks ──
    target_roles = _safe_json(profile.target_roles, [])
    target_locations = _safe_json(profile.target_locations, [])
    profile_skills = _safe_json(profile.skills, [])

    # Need at least some profile data to give meaningful advice
    if not target_roles and not profile_skills and not profile.resume_text:
        return {"advisor": None, "reason": "Add target roles, skills, or upload a resume before requesting analysis"}

    all_jobs = db.query(Job).filter(Job.profile_id == profile_id).all()

    liked = [j for j in all_jobs if j.status in ("liked", "shortlisted")]
    passed = [j for j in all_jobs if j.status == "passed"]
    high_score = [j for j in all_jobs if (j.match_score or 0) >= 65]
    low_score = [j for j in all_jobs if (j.match_score or 0) < 40]

    # Build job patterns summary
    liked_summary = "\n".join([
        f"  - {j.title} at {j.company} (score: {j.match_score or 0:.0f}, seniority: {j.seniority_level or '?'})"
        for j in sorted(liked, key=lambda x: -(x.match_score or 0))[:10]
    ]) if liked else "None yet"

    passed_summary = "\n".join([
        f"  - {j.title} at {j.company} (score: {j.match_score or 0:.0f}, seniority: {j.seniority_level or '?'})"
        for j in sorted(passed, key=lambda x: -(x.match_score or 0))[:8]
    ]) if passed else "None yet"

    # Score distribution
    scores = [j.match_score for j in all_jobs if j.match_score]
    avg_score = sum(scores) / len(scores) if scores else 0

    # Seniority distribution in found jobs
    seniority_dist = {}
    for j in all_jobs:
        lvl = (j.seniority_level or "unknown").lower()
        seniority_dist[lvl] = seniority_dist.get(lvl, 0) + 1

    # Skills frequency in ALL jobs vs profile
    job_skills_freq = {}
    for j in all_jobs:
        text = f"{j.description or ''} {j.requirements or ''}".lower()
        for skill in profile_skills:
            if skill.lower() in text:
                job_skills_freq[skill] = job_skills_freq.get(skill, 0) + 1

    # Skills frequency in HIGH-scoring jobs (65+) — shows what skills matter most
    high_score_skills_freq = {}
    for j in high_score:
        text = f"{j.description or ''} {j.requirements or ''}".lower()
        for skill in profile_skills:
            if skill.lower() in text:
                high_score_skills_freq[skill] = high_score_skills_freq.get(skill, 0) + 1

    # What high-scoring jobs have in common vs low-scoring ones
    high_score_titles = [j.title for j in high_score[:10]]
    low_score_titles = [j.title for j in low_score[:10]]
    high_score_companies = [j.company for j in high_score[:10]]

    # Gather answered profile questions for context
    answered_qs = db.query(ProfileQuestion).filter(
        ProfileQuestion.profile_id == profile_id,
        ProfileQuestion.is_answered == True,
    ).all()
    qa_context = "\n".join([f"Q: {q.question}\nA: {q.answer}" for q in answered_qs]) if answered_qs else "None yet"

    # Additional info from profile
    additional = _safe_json(profile.additional_info, {})

    prompt = f"""You are a senior executive career coach with deep expertise in career trajectory analysis, ATS optimization, and strategic job search planning.
Provide an HONEST, data-driven assessment with ACTIONABLE paths forward. Be direct but constructive — your job is to help them find the RIGHT role AND create a concrete plan to get there.

Consider the candidate's FULL career arc: where they started, how they progressed, and where they're naturally positioned to go next. Analyze patterns in the jobs they liked vs passed to understand their true preferences (which may differ from stated preferences).

TODAY'S DATE: {datetime.utcnow().strftime('%B %d, %Y')}

CANDIDATE PROFILE:
Name: {profile.name or 'Unknown'}
Current Seniority Level (self-assessed): {profile.seniority_level or 'Not specified'}
Target Roles: {json.dumps(target_roles)}
Experience: {profile.experience_years or '?'} years
Skills: {json.dumps(profile_skills)}
Location: {profile.location or 'Not specified'}
Target Locations: {json.dumps(target_locations)}
Salary Range: ${f'{profile.min_salary:,}' if profile.min_salary else '?'} - ${f'{profile.max_salary:,}' if profile.max_salary else '?'}
Remote Preference: {profile.remote_preference or 'Not specified'}
Search Tiers Down: {profile.search_tiers_down or 0} (looking {profile.search_tiers_down or 0} levels below target)
Search Tiers Up: {profile.search_tiers_up or 0} (looking {profile.search_tiers_up or 0} levels above target)
{f'Profile Summary: {profile.profile_summary}' if profile.profile_summary else ''}
{f'Career Trajectory: {profile.career_trajectory}' if profile.career_trajectory else ''}
{f'Leadership Style: {profile.leadership_style}' if profile.leadership_style else ''}
{f'Strengths: {profile.strengths}' if profile.strengths else ''}
{f'Growth Areas: {profile.growth_areas}' if profile.growth_areas else ''}
{f'Values: {profile.values}' if profile.values else ''}
{f'Deal Breakers: {profile.deal_breakers}' if profile.deal_breakers else ''}
{f'Ideal Culture: {profile.ideal_culture}' if profile.ideal_culture else ''}
{f'Industry Preferences: {profile.industry_preferences}' if profile.industry_preferences else ''}
{f'Resume (detailed): {profile.resume_text[:4000]}' if profile.resume_text else 'Resume: Not uploaded'}

PROFILE Q&A (candidate's own answers):
{qa_context[:2000]}

JOB SEARCH DATA ({len(all_jobs)} total jobs found):
- Average match score: {avg_score:.1f}/100
- High matches (65+): {len(high_score)} | Low matches (<40): {len(low_score)}
- Liked/Shortlisted: {len(liked)} | Passed: {len(passed)}
- Seniority distribution in found jobs: {json.dumps(seniority_dist)}

LIKED/SHORTLISTED JOBS (what they gravitate toward):
{liked_summary}

PASSED JOBS (what they avoid):
{passed_summary}

HIGH-SCORING JOB TITLES: {json.dumps(high_score_titles) if high_score_titles else 'None yet'}
HIGH-SCORING COMPANIES: {json.dumps(high_score_companies) if high_score_companies else 'None yet'}
LOW-SCORING JOB TITLES: {json.dumps(low_score_titles) if low_score_titles else 'None yet'}

SKILLS HIT RATE (across ALL jobs):
{json.dumps(job_skills_freq, indent=2) if job_skills_freq else 'No data yet'}

SKILLS IN HIGH-SCORING JOBS (65+):
{json.dumps(high_score_skills_freq, indent=2) if high_score_skills_freq else 'No data yet'}

IMPORTANT ANALYSIS REQUIREMENTS:
1. CAREER TRAJECTORY ASSESSMENT: Based on their resume and career history, assess whether their target roles are realistic as a NEXT step. Have they held roles that naturally lead to their targets? Are they trying to skip levels?
2. TARGET REALISM: Are their salary expectations realistic for their market + experience level? Is their seniority targeting accurate?
3. PROFILE GAPS: What's missing from their profile that would help find better matches?
4. CONCRETE SUGGESTIONS: Suggest specific profile field changes (target roles to add/remove, salary adjustments, seniority level changes, new skills to add, search tier adjustments).
5. PATTERN ANALYSIS: What do high-scoring jobs have in common that low-scoring jobs lack? What do liked jobs reveal about true preferences?
6. ATS OPTIMIZATION: What specific keywords should they add to their resume/profile to pass ATS screening for their target roles?
7. NETWORKING & TARGETING: Suggest specific types of people, companies, and industries to target.
8. ACTION PLAN: Create a concrete 30-60-90 day plan with specific, measurable actions.

Return JSON:
{{
    "overall_assessment": "3-4 sentence honest, constructive assessment of their career positioning and search effectiveness. Focus on actionable paths, not just critique.",
    "career_trajectory_analysis": {{
        "current_level": "your assessment of their actual seniority level based on resume/experience (entry|mid|senior|director|vp|c-suite)",
        "target_realism": "realistic|stretch|significant_stretch|unrealistic",
        "trajectory_narrative": "2-3 sentences analyzing their career arc and whether the target roles are a natural next step",
        "gap_to_target": "What specific experience/credentials are missing for their target roles",
        "recommended_level": "The seniority level you'd recommend they primarily target"
    }},
    "ambition_assessment": {{
        "verdict": "too_high|just_right|too_low|mixed",
        "explanation": "Why you think their targeting is too ambitious, too conservative, or appropriate",
        "confidence": <0-100 how confident you are in this assessment>
    }},
    "search_strategy": {{
        "verdict": "on_track|needs_adjustment|significantly_off",
        "explanation": "1-2 sentences explaining why"
    }},
    "profile_suggestions": [
        {{
            "field": "target_roles|min_salary|max_salary|seniority_level|search_tiers_down|search_tiers_up|skills|target_locations|remote_preference|profile_summary",
            "current_value": "the current value (for arrays use JSON array, for scalars use the value)",
            "suggested_value": "the COMPLETE new value (for target_roles/skills/target_locations: a JSON ARRAY of strings like [\"Role A\", \"Role B\"]. For salary: integer like 195000. For seniority: a string like \"director\")",
            "reason": "why this change would improve their search"
        }}
    ],
    "roles_to_consider": [
        "Specific role titles they should consider that they may not have thought of"
    ],
    "market_fit_score": <0-100>,
    "resume_feedback": ["specific resume improvements referencing actual content"],
    "skills_to_highlight": ["skills to emphasize based on high-scoring job patterns"],
    "skills_to_develop": ["gap skills to acquire"],
    "positioning_tips": ["how to better position themselves"],
    "quick_wins": ["immediate actionable items"],
    "action_plan": {{
        "30_days": ["specific action 1 for first month", "action 2"],
        "60_days": ["specific action 1 for second month", "action 2"],
        "90_days": ["specific action 1 for third month", "action 2"]
    }},
    "networking_targets": ["specific types of people, groups, or organizations to connect with"],
    "keywords_for_ats": ["specific keywords to add to resume/LinkedIn for ATS optimization"],
    "industry_targets": ["specific industries to focus applications on based on job data"],
    "companies_to_target": ["specific company names to prioritize based on job patterns and culture fit"],
    "red_flags_in_profile": ["things in their profile/resume that might hurt applications"],
    "differentiators": ["unique selling points this candidate should emphasize to stand out"],
    "questions_to_explore": [
        "Questions you'd want answered to refine your advice further (these will be used to generate profile questions)"
    ]
}}

BE SPECIFIC: Reference their actual resume content, job titles, companies, and data. Don't give generic career advice."""

    try:
        advisor = await asyncio.wait_for(
            ai_generate_json(prompt, max_tokens=4000, model_tier="deep"),
            timeout=55.0
        )
        if isinstance(advisor, list):
            advisor = None
        if isinstance(advisor, dict) and "overall_assessment" not in advisor:
            advisor = None
        if not advisor:
            advisor = await asyncio.wait_for(
                ai_generate_json(prompt, max_tokens=4000, model_tier="balanced"),
                timeout=55.0
            )
            if isinstance(advisor, list) or (isinstance(advisor, dict) and "overall_assessment" not in advisor):
                advisor = None
    except Exception as e:
        logger.warning(f"Search advisor generation failed: {e}", exc_info=True)
        advisor = None

    if advisor is None:
        return {"advisor": None, "reason": "AI analysis could not be generated — try again in a moment"}

    # Store advisor insights on the profile so the scorer can use them for boosting
    try:
        advisor_cache = {}
        for key in ("roles_to_consider", "keywords_for_ats", "industry_targets",
                     "companies_to_target", "skills_to_highlight", "skills_to_develop"):
            if advisor.get(key):
                advisor_cache[key] = advisor[key]
        if advisor_cache:
            profile.advisor_data = json.dumps(advisor_cache)
            db.commit()
    except Exception as e:
        logger.warning(f"Failed to cache advisor data for scorer: {e}")

    # If the advisor generated questions to explore, auto-create profile questions
    try:
        if advisor.get("questions_to_explore"):
            for q_text in advisor["questions_to_explore"][:5]:
                existing = db.query(ProfileQuestion).filter(
                    ProfileQuestion.profile_id == profile_id,
                    ProfileQuestion.question == q_text,
                ).first()
                if not existing:
                    pq = ProfileQuestion(
                        profile_id=profile_id,
                        question=q_text,
                        category="advisor",
                        priority=8,
                    )
                    db.add(pq)
            db.commit()
    except Exception as e:
        logger.warning(f"Failed to save advisor questions: {e}")

    return {"advisor": advisor}


@router.post("/profiles/{profile_id}/apply-advisor-suggestion")
async def apply_advisor_suggestion(profile_id: int, data: dict, db: Session = Depends(get_db)):
    """Apply a specific profile change suggested by the AI advisor."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    field = data.get("field")
    value = data.get("value")
    if not field:
        raise HTTPException(400, "Missing field")

    allowed_fields = {
        "seniority_level", "min_salary", "max_salary",
        "search_tiers_down", "search_tiers_up", "remote_preference",
    }
    # Text fields the AI advisor can update (profile narrative fields)
    text_fields = {
        "profile_summary", "career_trajectory", "leadership_style",
        "strengths", "growth_areas", "ideal_culture",
    }
    # These fields need JSON encoding
    json_fields = {"target_roles", "skills", "target_locations",
                   "values", "deal_breakers", "industry_preferences"}

    if field in allowed_fields:
        if field in ("min_salary", "max_salary", "search_tiers_down", "search_tiers_up"):
            # Strip formatting like "$180,000" → 180000
            if isinstance(value, str):
                value = re.sub(r'[^\d.]', '', value)
            value = int(float(value)) if value is not None and str(value).strip() else None
        setattr(profile, field, value)
    elif field in text_fields:
        # Free-text fields — just set the string value
        setattr(profile, field, str(value) if value else None)
    elif field in json_fields:
        # Normalize to a proper list
        if isinstance(value, str):
            # Try JSON parse first (AI sometimes sends stringified arrays)
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    value = parsed
            except (json.JSONDecodeError, TypeError):
                # Split comma-separated string
                value = [v.strip() for v in value.split(',') if v.strip()]
        if isinstance(value, list):
            # Filter out narrative instructions that aren't actual values
            value = [v for v in value if isinstance(v, str) and len(v) < 100
                     and not v.lower().startswith('remove ')
                     and not v.lower().startswith('add ')
                     and not v.lower().startswith('keep ')]
            setattr(profile, field, json.dumps(value))
        else:
            setattr(profile, field, json.dumps([value]) if value else json.dumps([]))
    else:
        raise HTTPException(400, f"Cannot update field: {field}")

    profile.updated_at = datetime.utcnow()
    db.commit()

    return {"message": f"Updated {field}", "field": field, "new_value": value}


# ── Insights / Summary ──────────────────────────────────────────────────

@router.get("/profiles/{profile_id}/insights")
async def get_insights(profile_id: int, db: Session = Depends(get_db)):
    """Generate insights and trends across all jobs for the Summary tab.

    Returns stats immediately. If AI insights are needed, launches a background
    task and includes a task_id for the AI portion. The frontend can poll for
    the AI insights while already displaying the stats.
    """
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    try:
        return await _build_insights(profile_id, profile, db)
    except Exception as e:
        logger.error(f"Insights generation crashed: {e}", exc_info=True)
        return {
            "total_jobs": 0, "pending": 0, "liked": 0, "shortlisted": 0,
            "passed": 0, "applied": 0, "applications": 0,
            "top_companies": [], "seniority_distribution": {},
            "location_distribution": {}, "source_distribution": {},
            "remote_distribution": {}, "salary_stats": {},
            "score_stats": {}, "app_status_distribution": {},
            "ai_insights": None,
            "error": f"{type(e).__name__}: {str(e)[:200]}"
        }


async def _build_insights(profile_id: int, profile, db: Session):
    """Inner insights builder — wrapped so caller catches any crash."""
    # Gather stats
    all_jobs = db.query(Job).filter(Job.profile_id == profile_id).all()
    pending = [j for j in all_jobs if j.status == "pending"]
    liked = [j for j in all_jobs if j.status == "liked"]
    shortlisted = [j for j in all_jobs if j.status == "shortlisted"]
    passed = [j for j in all_jobs if j.status == "passed"]
    applied_jobs = [j for j in all_jobs if j.status in ("liked", "applied")]

    # Company frequency
    company_counts = {}
    for j in all_jobs:
        company_counts[j.company] = company_counts.get(j.company, 0) + 1
    top_companies = sorted(company_counts.items(), key=lambda x: -x[1])[:10]

    # Seniority distribution
    seniority_dist = {}
    for j in all_jobs:
        level = j.seniority_level or "unknown"
        seniority_dist[level] = seniority_dist.get(level, 0) + 1

    # Location distribution
    location_dist = {}
    for j in all_jobs:
        loc = j.location or "Unknown"
        loc = loc.split(",")[0].strip()  # Just city
        location_dist[loc] = location_dist.get(loc, 0) + 1
    top_locations = sorted(location_dist.items(), key=lambda x: -x[1])[:8]

    # Source distribution
    source_dist = {}
    for j in all_jobs:
        sources = json.loads(j.sources_seen or "[]") or [j.source or "unknown"]
        for s in sources:
            source_dist[s] = source_dist.get(s, 0) + 1

    # Salary ranges
    salaries = [j.salary_min for j in all_jobs if j.salary_min]
    salary_stats = {}
    if salaries:
        salary_stats = {
            "min": min(salaries),
            "max": max([j.salary_max or j.salary_min for j in all_jobs if j.salary_min]),
            "avg": int(sum(salaries) / len(salaries)),
            "count": len(salaries),
        }

    # Score distribution
    scores = [j.match_score for j in all_jobs if j.match_score is not None]
    score_stats = {}
    if scores:
        score_stats = {
            "avg": round(sum(scores) / len(scores), 1),
            "high": len([s for s in scores if s >= 70]),
            "mid": len([s for s in scores if 40 <= s < 70]),
            "low": len([s for s in scores if s < 40]),
        }

    # Remote type distribution
    remote_dist = {}
    for j in all_jobs:
        rt = j.remote_type or "unknown"
        remote_dist[rt] = remote_dist.get(rt, 0) + 1

    # Applications stats
    apps = db.query(Application).filter(Application.profile_id == profile_id).all()
    app_status_dist = {}
    for a in apps:
        app_status_dist[a.status] = app_status_dist.get(a.status, 0) + 1

    # Skills coverage analysis: which user skills appear in what % of jobs
    profile_skills = _safe_json(profile.skills, [])
    skills_coverage = {}
    if profile_skills and all_jobs:
        for skill in profile_skills:
            hits = sum(1 for j in all_jobs if skill.lower() in f"{j.description or ''} {j.requirements or ''}".lower())
            skills_coverage[skill] = round(hits / len(all_jobs) * 100, 1)

    # Trending companies (multiple openings)
    trending_companies = [{"name": c, "count": n} for c, n in top_companies if n >= 2]

    # Response/application rate
    response_rate = None
    if apps:
        responded = sum(1 for a in apps if a.status not in ("draft", "submitted", "pending"))
        response_rate = round(responded / len(apps) * 100, 1) if apps else None

    # Salary range by seniority
    salary_by_seniority = {}
    for j in all_jobs:
        if j.salary_min and j.seniority_level:
            lvl = j.seniority_level.lower()
            if lvl not in salary_by_seniority:
                salary_by_seniority[lvl] = {"min": j.salary_min, "max": j.salary_max or j.salary_min, "count": 0}
            entry = salary_by_seniority[lvl]
            entry["min"] = min(entry["min"], j.salary_min)
            entry["max"] = max(entry["max"], j.salary_max or j.salary_min)
            entry["count"] = entry.get("count", 0) + 1

    # Location hotspots (locations with 3+ jobs)
    location_hotspots = [{"location": loc, "count": ct} for loc, ct in top_locations if ct >= 3]

    # AI-generated themes and market analysis — launch as background task
    ai_insights = None
    ai_insights_task_id = None
    if get_provider() != "none" and len(all_jobs) >= 3:
        # Check for already-running AI insights task
        existing_task = find_running_task("ai-insights", profile_id)
        if existing_task:
            ai_insights_task_id = existing_task
        else:
            # Build a compact summary of jobs for AI analysis (serialize now while we have DB access)
            job_summaries = []
            for j in sorted(all_jobs, key=lambda x: -(x.match_score or 0))[:30]:
                summary = f"- {j.title} at {j.company}"
                if j.location:
                    summary += f" ({j.location})"
                if j.salary_text:
                    summary += f" | {j.salary_text}"
                if j.seniority_level:
                    summary += f" | {j.seniority_level}"
                summary += f" | Score: {j.match_score:.0f}" if j.match_score else ""
                summary += f" | Status: {j.status}" if j.status != "pending" else ""
                job_summaries.append(summary)

            ai_context = {
                "name": profile.name or "Unknown",
                "seniority_level": profile.seniority_level or "Not specified",
                "target_roles": profile.target_roles or "Not specified",
                "skills": profile.skills or "Not specified",
                "location": profile.location or "Not specified",
                "min_salary": profile.min_salary,
                "max_salary": profile.max_salary,
                "job_summaries": job_summaries,
                "num_all": len(all_jobs),
                "num_liked": len(liked),
                "num_shortlisted": len(shortlisted),
                "num_passed": len(passed),
                "num_apps": len(apps),
                "score_stats": score_stats,
                "salary_stats": salary_stats,
                "top_locations": top_locations[:5],
                "seniority_dist": seniority_dist,
                "response_rate": response_rate,
                "skills_coverage": skills_coverage,
                "trending_companies": trending_companies,
                "salary_by_seniority": salary_by_seniority,
                "location_hotspots": location_hotspots,
            }
            ai_insights_task_id = run_background("ai-insights", profile_id,
                                                  _do_ai_insights, ai_context)

    return {
        "total_jobs": len(all_jobs),
        "pending": len(pending),
        "liked": len(liked),
        "shortlisted": len(shortlisted),
        "passed": len(passed),
        "applied": len(applied_jobs),
        "applications": len(apps),
        "top_companies": [{"name": c, "count": n} for c, n in top_companies],
        "seniority_distribution": seniority_dist,
        "location_distribution": dict(top_locations),
        "source_distribution": source_dist,
        "remote_distribution": remote_dist,
        "salary_stats": salary_stats,
        "score_stats": score_stats,
        "app_status_distribution": app_status_dist,
        "ai_insights": ai_insights,
        "ai_insights_task_id": ai_insights_task_id,
        "skills_coverage": skills_coverage,
        "trending_companies": trending_companies,
        "response_rate": response_rate,
        "salary_by_seniority": salary_by_seniority,
        "location_hotspots": location_hotspots,
    }


async def _do_ai_insights(ctx: dict):
    """Background worker for AI insights generation."""
    sal_min = ctx["salary_stats"].get('min', 0) or 0
    sal_max = ctx["salary_stats"].get('max', 0) or 0
    sal_avg = ctx["salary_stats"].get('avg', 0) or 0

    prompt = f"""You are a career intelligence analyst. Analyze this job search data and provide strategic, actionable insights. Return a single JSON OBJECT (not an array).

TODAY'S DATE: {datetime.utcnow().strftime('%B %d, %Y')}

CANDIDATE: {ctx['name']}
SENIORITY: {ctx['seniority_level']}
TARGET ROLES: {ctx['target_roles']}
SKILLS: {ctx['skills']}
LOCATION: {ctx['location']}
SALARY EXPECTATION: ${f'{ctx["min_salary"]:,}' if ctx['min_salary'] else '?'} - ${f'{ctx["max_salary"]:,}' if ctx['max_salary'] else '?'}

JOB SEARCH DATA ({ctx['num_all']} jobs found, {ctx['num_liked']} liked, {ctx['num_shortlisted']} shortlisted, {ctx['num_passed']} passed):
{chr(10).join(ctx['job_summaries'])}

STATS:
- Average match score: {ctx['score_stats'].get('avg', 'N/A')}
- Salary range: ${sal_min:,} - ${sal_max:,} (avg ${sal_avg:,})
- Top locations: {', '.join([f'{loc} ({ct})' for loc, ct in ctx['top_locations']])}
- Seniority breakdown: {json.dumps(ctx['seniority_dist'])}
- Applications: {ctx['num_apps']} total
{f'- Application response rate: {ctx["response_rate"]}%' if ctx['response_rate'] is not None else ''}
- Skills coverage in jobs: {json.dumps(ctx['skills_coverage']) if ctx['skills_coverage'] else 'No data'}
- Trending companies (2+ openings): {json.dumps([c["name"] for c in ctx['trending_companies']]) if ctx['trending_companies'] else 'None'}
- Salary by seniority: {json.dumps(ctx['salary_by_seniority']) if ctx['salary_by_seniority'] else 'No data'}
- Location hotspots: {json.dumps([h["location"] for h in ctx['location_hotspots']]) if ctx['location_hotspots'] else 'No data'}

ANALYSIS REQUIREMENTS:
1. Assess the candidate's competitive position in the current market (0-100 score)
2. Identify areas/niches the candidate hasn't explored that could be good fits
3. Provide market timing advice — is this a good time to be searching?
4. Analyze which companies are hiring similar roles (competitor landscape)
5. Identify the top skills appearing in their target jobs
6. Provide specific application strategy advice (which jobs to prioritize and why)

Return JSON:
{{
    "market_summary": "2-3 sentence summary of the current market landscape for this candidate",
    "themes": ["theme 1 - a pattern or trend observed across multiple jobs", "theme 2", "theme 3", "theme 4"],
    "opportunities": ["specific actionable opportunity or insight", "another opportunity"],
    "risks": ["market risk or concern to watch out for"],
    "salary_insight": "1-2 sentences about compensation trends vs candidate expectations",
    "demand_signals": ["signal indicating strong/weak demand in their space"],
    "recommendations": ["strategic recommendation 1", "recommendation 2", "recommendation 3"],
    "skill_gaps": ["skills that appear frequently in jobs but may be gaps for this candidate"],
    "hot_companies": ["companies that seem to be actively hiring in their space"],
    "market_position": <0-100 score of how competitive this candidate is in the current market>,
    "underexplored_areas": ["niche or area the candidate hasn't considered but could be a good fit"],
    "timing_advice": "1-2 sentences about whether now is a good time to apply and any timing considerations",
    "competitor_landscape": "1-2 sentences about who else is hiring similar roles and what the competition looks like",
    "top_skills_in_demand": ["most frequently requested skills in their target job postings"],
    "application_strategy": "specific advice on which jobs to prioritize and how to sequence applications"
}}"""

    try:
        ai_insights = await asyncio.wait_for(
            ai_generate_json(prompt, max_tokens=1800, model_tier="deep"),
            timeout=50.0
        )
        if isinstance(ai_insights, list):
            logger.warning("AI insights returned a list instead of dict, retrying...")
            ai_insights = None
        if isinstance(ai_insights, dict) and "market_summary" not in ai_insights:
            logger.warning("AI insights missing market_summary key, retrying...")
            ai_insights = None
        if not ai_insights:
            retry_prompt = prompt + "\n\nCRITICAL: Return a single JSON OBJECT (not an array). The response must start with { and end with }."
            ai_insights = await asyncio.wait_for(
                ai_generate_json(retry_prompt, max_tokens=1800, model_tier="balanced"),
                timeout=50.0
            )
            if isinstance(ai_insights, list) or (isinstance(ai_insights, dict) and "market_summary" not in ai_insights):
                ai_insights = None
    except Exception as e:
        logger.warning(f"AI insights generation failed: {e}")
        ai_insights = None

    return {"ai_insights": ai_insights}


# ── Stats ─────────────────────────────────────────────────────────────────

@router.get("/profiles/{profile_id}/apply-readiness")
def get_apply_readiness(profile_id: int, db: Session = Depends(get_db)):
    """Check if profile has everything needed for auto-applications."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    categories = []

    def add_category(name, checks_list):
        passed = sum(1 for c in checks_list if c["passed"])
        categories.append({
            "category": name,
            "checks": checks_list,
            "passed": passed,
            "total": len(checks_list),
        })

    def check(name, passed, detail=""):
        return {"name": name, "passed": passed, "detail": detail}

    # ── Profile Basics ─────────────────────────────────────────────────
    basics = []
    basics.append(check("Full name", bool(profile.name), profile.name or "Missing"))
    basics.append(check("Email address", bool(profile.email), profile.email or "Required for applications"))
    basics.append(check("Phone number", bool(profile.phone), profile.phone or "Many applications require this"))
    basics.append(check("Location", bool(profile.location), profile.location or "Needed for location-based filtering"))

    has_resume = bool(profile.resume_path) or bool(profile.resume_text)
    basics.append(check("Resume uploaded", has_resume, "Most applications require a resume file"))

    skills = _safe_json(profile.skills, [])
    basics.append(check("Skills listed (3+)", len(skills) >= 3, f"{len(skills)} skills" if skills else "Add at least 3 skills"))

    basics.append(check("Years of experience", profile.experience_years is not None, f"{profile.experience_years} years" if profile.experience_years else "Helps with seniority matching"))

    roles = _safe_json(profile.target_roles, [])
    basics.append(check("Target roles defined", len(roles) >= 1, f"{len(roles)} roles" if roles else "Define what you're looking for"))

    basics.append(check("Profile analyzed by AI", bool(profile.profile_analyzed), "Enables smarter cover letters and matching"))

    basics.append(check("Cover letter style notes", bool(profile.cover_letter_template), "Optional but improves cover letter quality"))
    add_category("Profile Basics", basics)

    # ── Profile Quality ────────────────────────────────────────────────
    quality = []
    summary = profile.profile_summary or ""
    quality.append(check("Profile summary", len(summary) > 50, f"{len(summary)} chars" if summary else "Run AI analysis to generate"))

    quality.append(check("Career trajectory", bool(profile.career_trajectory), "Defined" if profile.career_trajectory else "Run AI analysis to generate"))

    quality.append(check("Leadership style", bool(profile.leadership_style), "Defined" if profile.leadership_style else "Run AI analysis to generate"))

    strengths = _safe_json(profile.strengths, [])
    quality.append(check("Strengths identified (3+)", len(strengths) >= 3, f"{len(strengths)} strengths" if strengths else "Run AI analysis to generate"))

    industries = _safe_json(profile.industry_preferences, [])
    quality.append(check("Industry preference", len(industries) >= 1, f"{len(industries)} industries" if industries else "Run AI analysis or add manually"))

    quality.append(check("Seniority level set", bool(profile.seniority_level), profile.seniority_level or "Run AI analysis to determine"))
    add_category("Profile Quality", quality)

    # ── Search Strategy ────────────────────────────────────────────────
    strategy = []

    # Check if user has performed at least one search (has any jobs)
    job_count = db.query(Job).filter(Job.profile_id == profile_id).count()
    strategy.append(check("Performed a job search", job_count > 0, f"{job_count} jobs found" if job_count > 0 else "Search for jobs to start"))

    # Has reviewed AI advisor (check if profile_analyzed, which is set after analysis)
    strategy.append(check("AI advisor reviewed", bool(profile.profile_analyzed), "Analysis complete" if profile.profile_analyzed else "Get AI analysis from Summary > AI Advisor"))

    # Has at least 5 jobs scored above 60
    high_score_count = db.query(Job).filter(
        Job.profile_id == profile_id,
        Job.match_score >= 60
    ).count()
    strategy.append(check("5+ strong matches (score 60+)", high_score_count >= 5, f"{high_score_count} jobs above 60" if high_score_count > 0 else "Search and score more jobs"))

    # Salary expectations set
    strategy.append(check("Salary expectations set", bool(profile.min_salary and profile.min_salary > 0), f"${profile.min_salary:,}+" if profile.min_salary else "Set minimum salary"))

    # Location preferences
    locations = _safe_json(profile.target_locations, [])
    strategy.append(check("Location preferences set", len(locations) >= 1, f"{len(locations)} locations" if locations else "Add target locations"))

    # Remote preference is intentional
    strategy.append(check("Remote preference set", profile.remote_preference and profile.remote_preference != "any", profile.remote_preference or "Set a specific preference"))
    add_category("Search Strategy", strategy)

    # ── Application Quality ────────────────────────────────────────────
    app_quality = []

    # Resume recency (uploaded in last 90 days)
    resume_recent = False
    resume_detail = "No resume uploaded"
    if has_resume and profile.updated_at:
        days_old = (datetime.utcnow() - profile.updated_at).days
        resume_recent = days_old <= 90
        resume_detail = f"Updated {days_old} days ago" if resume_recent else f"Last updated {days_old} days ago - consider refreshing"
    app_quality.append(check("Resume is recent (90 days)", resume_recent, resume_detail))

    # Skills match market demand (check if skills overlap with job requirements)
    skills_in_demand = False
    skills_detail = "No skills or jobs to compare"
    if skills and job_count > 0:
        # Check how many of user's skills appear in job descriptions/requirements
        skill_hits = 0
        sample_jobs = db.query(Job).filter(Job.profile_id == profile_id).limit(50).all()
        for skill in skills:
            for j in sample_jobs:
                text = f"{j.requirements or ''} {j.description or ''}".lower()
                if skill.lower() in text:
                    skill_hits += 1
                    break
        pct = int(skill_hits / len(skills) * 100) if skills else 0
        skills_in_demand = pct >= 50
        skills_detail = f"{skill_hits}/{len(skills)} skills found in job listings ({pct}%)"
    app_quality.append(check("Skills match market demand", skills_in_demand, skills_detail))

    # Target roles are realistic (jobs actually exist for them)
    roles_realistic = False
    roles_detail = "No target roles set"
    if roles and job_count > 0:
        role_match_count = 0
        for role in roles:
            matching = db.query(Job).filter(
                Job.profile_id == profile_id,
                Job.title.ilike(f"%{role}%")
            ).count()
            if matching > 0:
                role_match_count += 1
        roles_realistic = role_match_count > 0
        roles_detail = f"{role_match_count}/{len(roles)} target roles found in listings" if role_match_count > 0 else "No matching jobs found - consider broadening"
    app_quality.append(check("Target roles match market", roles_realistic, roles_detail))
    add_category("Application Quality", app_quality)

    # ── Aggregate ──────────────────────────────────────────────────────
    all_checks = []
    for cat in categories:
        all_checks.extend(cat["checks"])

    passed = sum(1 for c in all_checks if c["passed"])
    total = len(all_checks)
    score = int((passed / total) * 100) if total > 0 else 0

    return {
        "score": score,
        "passed": passed,
        "total": total,
        "ready": score >= 70,
        "categories": categories,
        "checks": all_checks,  # backward compat
    }


@router.get("/ai-provider")
def get_ai_provider():
    """Check which AI provider is active."""
    provider = get_provider()
    return {"provider": provider}


# ── Suggestions & Skills Grooming ─────────────────────────────────────────

@router.get("/profiles/{profile_id}/suggestions")
def get_field_suggestions(
    profile_id: int,
    field: str = Query(..., description="Field to get suggestions for: skills, roles, locations"),
    q: str = Query("", description="Partial text to filter suggestions"),
    db: Session = Depends(get_db),
):
    """Return autocomplete suggestions for profile fields based on job market data."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    q_lower = q.strip().lower()
    all_jobs = db.query(Job).filter(Job.profile_id == profile_id).all()

    if field == "skills":
        # Extract skill-like terms from job requirements/descriptions
        skill_freq = {}
        for j in all_jobs:
            text = f"{j.requirements or ''} {j.description or ''}".lower()
            # Look for common skill patterns in the text
            for chunk in re.split(r'[,;•·\n]', f"{j.requirements or ''}"):
                chunk = chunk.strip().strip('- •·').strip()
                if 2 <= len(chunk) <= 50 and not any(w in chunk.lower() for w in ['experience', 'years', 'ability to', 'must have', 'required', 'preferred', 'strong', 'excellent', 'proven']):
                    # Normalize to title case for consistency
                    normalized = chunk.strip().title() if len(chunk) < 30 else chunk.strip()
                    skill_freq[normalized] = skill_freq.get(normalized, 0) + 1

        # Also add well-known skills that appear frequently in descriptions
        known_skills = [
            "Risk Management", "Cloud Security", "AWS", "Azure", "GCP",
            "Python", "JavaScript", "TypeScript", "Java", "Go", "Rust", "C#",
            "Kubernetes", "Docker", "Terraform", "CI/CD", "DevOps", "DevSecOps",
            "NIST CSF", "ISO 27001", "SOC 2", "COBIT", "ITIL", "PCI DSS",
            "Incident Response", "Threat Intelligence", "SIEM", "Splunk",
            "Penetration Testing", "Vulnerability Management", "IAM",
            "Zero Trust", "Network Security", "Endpoint Security",
            "Data Privacy", "GDPR", "PIPEDA", "SOX Compliance",
            "Machine Learning", "AI", "NLP", "Data Science", "SQL", "NoSQL",
            "React", "Angular", "Vue.js", "Node.js", "FastAPI", "Django",
            "Agile", "Scrum", "SDLC", "Project Management", "PMP",
            "Stakeholder Management", "Budget Management", "Vendor Management",
            "Strategic Planning", "Executive Communication", "Board Reporting",
            "Team Leadership", "People Management", "Change Management",
            "M&A Integration", "Digital Transformation", "Enterprise Architecture",
        ]
        for skill in known_skills:
            for j in all_jobs:
                text = f"{j.requirements or ''} {j.description or ''}".lower()
                if skill.lower() in text:
                    skill_freq[skill] = skill_freq.get(skill, 0) + 1
                    break

        # Filter existing skills out
        existing = set(s.lower() for s in _safe_json(profile.skills, []))
        suggestions = [
            {"value": k, "count": v}
            for k, v in sorted(skill_freq.items(), key=lambda x: -x[1])
            if k.lower() not in existing and (not q_lower or q_lower in k.lower())
        ][:25]
        return {"suggestions": suggestions}

    elif field == "roles":
        # Extract unique job titles from found jobs, filtering scraper artifacts
        junk_titles = {"skip to filters", "browse", "best match", "sort by", "date posted",
                       "sign in", "log in", "search", "filter", "results", "next", "previous",
                       "view all", "load more", "show more", "apply now"}
        title_freq = {}
        for j in all_jobs:
            title = (j.title or "").strip()
            # Skip scraper artifacts: junk titles, titles with site prefixes, too short
            if (title and 5 <= len(title) < 80
                    and title.lower() not in junk_titles
                    and not any(p in title for p in ['CivicJobs.ca', 'CareerBeacon', 'Indeed.com', 'LinkedIn'])):
                title_freq[title] = title_freq.get(title, 0) + 1
        existing = set(r.lower() for r in _safe_json(profile.target_roles, []))
        suggestions = [
            {"value": k, "count": v}
            for k, v in sorted(title_freq.items(), key=lambda x: -x[1])
            if k.lower() not in existing and (not q_lower or q_lower in k.lower())
        ][:25]
        return {"suggestions": suggestions}

    elif field == "locations":
        # Extract unique locations from found jobs
        loc_freq = {}
        for j in all_jobs:
            loc = (j.location or "").strip()
            if loc and len(loc) < 80:
                loc_freq[loc] = loc_freq.get(loc, 0) + 1
        existing = set(l.lower() for l in _safe_json(profile.target_locations, []))
        suggestions = [
            {"value": k, "count": v}
            for k, v in sorted(loc_freq.items(), key=lambda x: -x[1])
            if k.lower() not in existing and (not q_lower or q_lower in k.lower())
        ][:25]
        return {"suggestions": suggestions}

    else:
        raise HTTPException(400, f"Unknown field: {field}")


@router.get("/profiles/{profile_id}/skill-demand")
def skill_demand(profile_id: int, db: Session = Depends(get_db)):
    """Lightweight endpoint: return demand percentages for each profile skill based on job postings."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    all_jobs = db.query(Job).filter(Job.profile_id == profile_id).all()
    if not all_jobs:
        return {"total_jobs": 0, "skill_hits": {}}

    profile_skills = _safe_json(profile.skills, [])
    skill_hits = {}
    for skill in profile_skills:
        count = 0
        for j in all_jobs:
            text = f"{j.title or ''} {j.description or ''} {j.requirements or ''}".lower()
            if skill.lower() in text:
                count += 1
        skill_hits[skill] = {"count": count, "pct": round(count / len(all_jobs) * 100, 1)}

    return {"total_jobs": len(all_jobs), "skill_hits": skill_hits}


@router.get("/profiles/{profile_id}/skills-audit")
async def skills_audit(profile_id: int, db: Session = Depends(get_db)):
    """Audit profile skills against actual job postings — find gaps, dead weight, and trending skills."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    all_jobs = db.query(Job).filter(Job.profile_id == profile_id).all()
    if not all_jobs:
        return {"audit": None, "reason": "No jobs found yet — search first"}

    profile_skills = _safe_json(profile.skills, [])

    # Compute hit rate for each profile skill
    skill_hits = {}
    for skill in profile_skills:
        count = 0
        for j in all_jobs:
            text = f"{j.title or ''} {j.description or ''} {j.requirements or ''}".lower()
            if skill.lower() in text:
                count += 1
        skill_hits[skill] = {"count": count, "pct": round(count / len(all_jobs) * 100, 1)}

    # Find skills in job postings NOT in profile (gap analysis)
    # Use AI if available for better extraction
    gap_skills = {}
    sample_reqs = "\n---\n".join([
        f"{j.title}: {(j.requirements or '')[:300]}"
        for j in sorted(all_jobs, key=lambda x: -(x.match_score or 0))[:20]
    ])

    if get_provider() != "none":
        prompt = f"""Analyze these job posting requirements and extract the most in-demand SKILLS that appear across multiple postings.

CANDIDATE'S CURRENT SKILLS: {json.dumps(profile_skills)}

JOB POSTINGS (top 20 by match score):
{sample_reqs[:4000]}

Return JSON:
{{
    "missing_high_demand": ["skills that appear in 5+ postings but candidate doesn't have - use standard industry terms"],
    "missing_moderate": ["skills in 2-4 postings the candidate is missing"],
    "low_value_skills": ["candidate skills that appear in <5% of postings (consider removing)"],
    "skill_categories": {{
        "technical": ["technical/tool skills to add"],
        "frameworks": ["frameworks, standards, certifications to add"],
        "leadership": ["leadership/management skills to add"],
        "domain": ["domain/industry expertise to add"]
    }},
    "recommended_additions": ["top 5-8 most impactful skills to add to profile, in priority order"],
    "recommended_removals": ["skills to consider removing (too generic, not in demand, or redundant)"]
}}

IMPORTANT: Only suggest skills using standard industry terminology (e.g. "NIST CSF" not "security frameworks", "AWS" not "cloud platforms").
Each skill should be a concise 1-4 word term that recruiters search for."""

        try:
            ai_audit = await ai_generate_json(prompt, max_tokens=1200, model_tier="balanced")
        except Exception as e:
            logger.warning(f"Skills audit AI failed: {e}")
            ai_audit = None
    else:
        ai_audit = None

    return {
        "total_jobs": len(all_jobs),
        "profile_skills": profile_skills,
        "skill_hits": skill_hits,
        "ai_audit": ai_audit,
    }


@router.get("/profiles/{profile_id}/stats")
def get_stats(profile_id: int, db: Session = Depends(get_db)):
    total = db.query(Job).filter(Job.profile_id == profile_id).count()
    pending = db.query(Job).filter(Job.profile_id == profile_id, Job.status == "pending").count()
    liked = db.query(Job).filter(Job.profile_id == profile_id, Job.status == "liked").count()
    shortlisted = db.query(Job).filter(Job.profile_id == profile_id, Job.status == "shortlisted").count()
    passed = db.query(Job).filter(Job.profile_id == profile_id, Job.status == "passed").count()
    applied = db.query(Job).filter(Job.profile_id == profile_id, Job.status == "applied").count()
    expired = db.query(Job).filter(Job.profile_id == profile_id, Job.status == "expired").count()
    apps = db.query(Application).filter(Application.profile_id == profile_id).count()
    ready = db.query(Application).filter(
        Application.profile_id == profile_id,
        Application.status == "ready",
    ).count()
    needs_input = db.query(Application).filter(
        Application.profile_id == profile_id,
        Application.status == "needs_input",
    ).count()

    # Baseball stats
    at_bats = total  # Total jobs seen
    hits = liked + applied + apps  # Jobs you engaged with positively
    strikeouts = passed  # Passed on
    walks = shortlisted  # On base waiting
    avg = round(hits / at_bats, 3) if at_bats > 0 else 0.0
    obp = round((hits + walks) / at_bats, 3) if at_bats > 0 else 0.0  # On-base percentage
    slg = round((applied * 2 + apps * 3) / at_bats, 3) if at_bats > 0 else 0.0  # Slugging
    ops = round(obp + slg, 3)  # OPS = OBP + SLG

    # Source distribution
    from sqlalchemy import func
    source_counts = dict(
        db.query(Job.source, func.count(Job.id))
        .filter(Job.profile_id == profile_id)
        .group_by(Job.source)
        .all()
    )

    return {
        "total_jobs": total,
        "pending_swipe": pending,
        "liked": liked,
        "shortlisted": shortlisted,
        "passed": passed,
        "applied": applied,
        "expired": expired,
        "applications": apps,
        "ready_to_submit": ready,
        "needs_input": needs_input,
        # Baseball stats
        "at_bats": at_bats,
        "hits": hits,
        "strikeouts": strikeouts,
        "walks": walks,
        "avg": avg,
        "obp": obp,
        "slg": slg,
        "ops": ops,
        "source_counts": source_counts,
    }


# ── Helpers ───────────────────────────────────────────────────────────────

def _job_completeness(j: Job) -> int:
    """Score 0-100 how complete a job's data is for presentation."""
    score = 0
    total = 0

    # Core fields (weighted heavily)
    total += 20; score += 20 if j.title else 0
    total += 15; score += 15 if j.company else 0
    total += 10; score += 10 if j.location else 0
    total += 15; score += 15 if (j.description and len(j.description) > 100) else 0

    # Enrichment fields
    total += 10; score += 10 if j.role_summary else 0
    total += 10; score += 10 if (j.salary_text or j.salary_min) else 0
    total += 5;  score += 5 if j.seniority_level else 0
    total += 5;  score += 5 if j.url else 0
    total += 5;  score += 5 if j.posted_date or j.closing_date else 0
    total += 5;  score += 5 if j.remote_type else 0

    return int((score / total) * 100) if total > 0 else 0


def _safe_json_list(val: str | None) -> list:
    """Safely parse a JSON list field, returning [] on any error."""
    if not val:
        return []
    try:
        result = json.loads(val)
        return result if isinstance(result, list) else [str(result)]
    except (json.JSONDecodeError, TypeError):
        # Field contains plain text (e.g. from AI suggestion) — wrap as single-item list
        return [s.strip().strip('"') for s in val.split(",") if s.strip()] if val else []


def _profile_dict(p: Profile) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "has_pin": bool(p.pin),
        "email": p.email,
        "phone": p.phone,
        "location": p.location,
        "target_roles": _safe_json_list(p.target_roles),
        "target_locations": _safe_json_list(p.target_locations),
        "min_salary": p.min_salary,
        "max_salary": p.max_salary,
        "remote_preference": p.remote_preference,
        "experience_years": p.experience_years,
        "skills": _safe_json_list(p.skills),
        "resume_uploaded": bool(p.resume_path),
        "has_resume_text": bool(p.resume_text),
        "cover_letter_template": p.cover_letter_template,
        "has_profile_doc": bool(p.raw_profile_doc),
        # Deep profile insights
        "profile_analyzed": p.profile_analyzed or False,
        "profile_summary": p.profile_summary,
        "career_trajectory": p.career_trajectory,
        "leadership_style": p.leadership_style,
        "industry_preferences": _safe_json_list(p.industry_preferences),
        "values": _safe_json_list(p.values),
        "deal_breakers": _safe_json_list(p.deal_breakers),
        "strengths": _safe_json_list(p.strengths),
        "growth_areas": _safe_json_list(p.growth_areas),
        "ideal_culture": p.ideal_culture,
        "seniority_level": p.seniority_level,
        "search_tiers_down": p.search_tiers_down or 0,
        "search_tiers_up": p.search_tiers_up or 0,
        "career_history": _safe_json(p.career_history, []),
    }


def _job_dict(j: Job, db: Session = None) -> dict:
    company_data = None
    if j.company_id and db:
        company = db.query(Company).filter(Company.id == j.company_id).first()
        if company:
            company_data = company_dict(company)

    return {
        "id": j.id,
        "title": j.title,
        "company": j.company,
        "company_id": j.company_id,
        "company_data": company_data,
        "location": j.location,
        "salary_min": j.salary_min,
        "salary_max": j.salary_max,
        "salary_text": j.salary_text,
        "salary_estimated": j.salary_estimated,
        "job_type": j.job_type,
        "remote_type": j.remote_type,
        "description": j.description,
        "url": j.url,
        "url_valid": j.url_valid,
        "source": j.source,
        "sources_seen": json.loads(j.sources_seen or "[]"),
        "match_score": j.match_score,
        "match_breakdown": json.loads(j.match_breakdown or "{}") if j.match_breakdown else None,
        "match_reasons": json.loads(j.match_reasons or "[]"),
        "status": j.status,
        "posted_date": j.posted_date,
        "closing_date": j.closing_date,
        "is_repost": j.is_repost,
        "seniority_level": j.seniority_level,
        "reports_to": j.reports_to,
        "team_size": j.team_size,
        "role_summary": j.role_summary,
        "red_flags": json.loads(j.red_flags or "[]"),
        "why_apply": json.loads(j.why_apply or "[]"),
        "enriched": j.enriched,
        "completeness": _job_completeness(j),
        "scraped_at": j.scraped_at.isoformat() if j.scraped_at else None,
        "first_seen": j.first_seen.isoformat() if j.first_seen else None,
        "last_seen": j.last_seen.isoformat() if j.last_seen else None,
        "created_at": j.created_at.isoformat() if j.created_at else None,
        # Deep research
        "deep_researched": j.deep_researched or False,
        "culture_insights": j.culture_insights,
        "interview_process": j.interview_process,
        "growth_opportunities": j.growth_opportunities,
        "day_in_life": j.day_in_life,
        "hiring_sentiment": j.hiring_sentiment,
        "research_sources": json.loads(j.research_sources or "[]") if j.research_sources else [],
        # AI synthesis: compact one-liner from deep research for card display
        "ai_synthesis": _build_ai_synthesis(j) if (j.deep_researched) else None,
    }


def _build_ai_synthesis(j: Job) -> str:
    """Build a compact one-line AI synthesis from deep research findings."""
    parts = []
    if j.culture_insights:
        # Take first sentence
        first = j.culture_insights.split(".")[0].strip()
        if first:
            parts.append(first)
    if j.hiring_sentiment:
        first = j.hiring_sentiment.split(".")[0].strip()
        if first and len(parts) == 0:
            parts.append(first)
    if j.growth_opportunities:
        first = j.growth_opportunities.split(".")[0].strip()
        if first and len(parts) <= 1:
            parts.append(first)
    return ". ".join(parts)[:200] + "." if parts else ""


def _application_dict(a: Application) -> dict:
    return {
        "id": a.id,
        "job_id": a.job_id,
        "job_title": a.job.title if a.job else "",
        "company": a.job.company if a.job else "",
        "status": a.status,
        "cover_letter": a.cover_letter,
        "agent_log": json.loads(a.agent_log or "[]"),
        "error_message": a.error_message,
        "applied_at": a.applied_at.isoformat() if a.applied_at else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _question_dict(q: AgentQuestion) -> dict:
    return {
        "id": q.id,
        "application_id": q.application_id,
        "question": q.question,
        "context": q.context,
        "is_answered": q.is_answered,
        "answer": q.answer,
    }


# ── Resume Improver ──────────────────────────────────────────────────────

@router.post("/profiles/{profile_id}/improve-resume")
async def improve_resume(profile_id: int, db: Session = Depends(get_db)):
    """Generate AI-powered resume improvement suggestions (background task)."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")
    if not profile.resume_text:
        raise HTTPException(400, "No resume text found. Upload or paste a resume first.")

    existing_task = find_running_task("improve-resume", profile_id)
    if existing_task:
        return {"task_id": existing_task, "status": "already_running"}

    task_id = run_background("improve-resume", profile_id, _improve_resume_worker, profile_id)
    return {"task_id": task_id, "status": "started"}


async def _improve_resume_worker(profile_id: int):
    """Background worker for resume improvement suggestions."""
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.id == profile_id).first()
        if not profile:
            return {"error": "Profile not found"}

        # Gather answered Q&A for context
        answered_qs = db.query(ProfileQuestion).filter(
            ProfileQuestion.profile_id == profile_id,
            ProfileQuestion.is_answered == True,
        ).all()
        qa_context = "\n".join([f"Q: {q.question}\nA: {q.answer}" for q in answered_qs])

        # Gather job descriptions for keyword analysis
        jobs = db.query(Job).filter(Job.profile_id == profile_id).limit(10).all()
        job_keywords_context = ""
        if jobs:
            descs = [j.description[:500] for j in jobs if j.description]
            job_keywords_context = f"\nSAMPLE JOB DESCRIPTIONS FROM THEIR SEARCH (use to identify missing keywords):\n" + "\n---\n".join(descs[:5])

        prompt = f"""You are an expert resume reviewer and ATS optimization specialist.
Analyze this resume and provide specific, actionable improvement suggestions.

RESUME:
{profile.resume_text[:6000]}

CANDIDATE CONTEXT:
- Target Roles: {profile.target_roles or 'Not specified'}
- Experience: {profile.experience_years or 'Unknown'} years
- Skills: {profile.skills or 'Not listed'}
- Location: {profile.location or 'Not specified'}
{f'Profile Summary: {profile.profile_summary}' if profile.profile_summary else ''}

INTERVIEW Q&A (use these insights to suggest improvements):
{qa_context[:3000] if qa_context else 'No interview answers yet.'}
{job_keywords_context[:2000]}

Provide your analysis as a JSON object with this structure:
{{
    "suggestions": [
        {{
            "section": "Which resume section this applies to (e.g. 'Professional Experience - Company Name', 'Skills', 'Summary')",
            "type": "quantify|add|reword|remove|reorder",
            "current": "The current text or description of what exists (or 'N/A' for additions)",
            "suggested": "Your specific suggested improvement — write out the actual new text",
            "reason": "Brief explanation of why this improves the resume"
        }}
    ],
    "overall_score": 0-100,
    "missing_keywords": ["keywords from target job descriptions missing from resume"],
    "ats_tips": ["specific ATS optimization tips for this resume"]
}}

RULES:
- Provide 8-15 specific suggestions, prioritized by impact
- For "quantify" type: identify vague statements and suggest specific metrics (ask the candidate if you don't know the number, use [X] placeholder)
- For "add" type: suggest missing sections, skills, or achievements based on Q&A answers
- For "reword" type: improve weak action verbs, passive voice, or unclear descriptions
- For "remove" type: identify outdated, redundant, or space-wasting content
- For "reorder" type: suggest better ordering of sections or bullet points
- missing_keywords: compare resume against job descriptions to find gaps
- ats_tips: focus on formatting, section headers, and keyword optimization
- overall_score: rate the resume 0-100 based on clarity, impact, ATS-friendliness, and completeness"""

        try:
            result = await ai_generate_json(prompt, max_tokens=3000, model_tier="balanced")
        except Exception as e:
            logger.error(f"Resume improvement AI failed: {e}", exc_info=True)
            result = None

        if not result:
            try:
                result = await ai_generate_json(prompt, max_tokens=3000, model_tier="flash")
            except Exception:
                pass

        if not result:
            return {"error": "AI generation failed"}

        # Normalize result
        if isinstance(result, dict):
            return {
                "suggestions": result.get("suggestions", []),
                "overall_score": result.get("overall_score", 0),
                "missing_keywords": result.get("missing_keywords", []),
                "ats_tips": result.get("ats_tips", []),
            }
        return {"error": "Unexpected AI response format"}
    finally:
        db.close()


# ── Email Integration ─────────────────────────────────────────────────────

@router.post("/profiles/{profile_id}/check-emails")
async def check_application_emails(profile_id: int, data: dict, db: Session = Depends(get_db)):
    """Process emails to find application status updates.

    Expects: {"emails": [{"subject": "...", "from": "...", "snippet": "...", "id": "..."}]}
    Returns: list of status updates found
    """
    from backend.services.email_monitor import (
        classify_email_basic, classify_email_ai,
        match_email_to_application, update_application_status,
    )

    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    emails = data.get("emails", [])
    updates = []

    for email in emails:
        subject = email.get("subject", "")
        from_addr = email.get("from", "")
        snippet = email.get("snippet", "")
        email_id = email.get("id", "")

        # Quick regex classification first
        classification = classify_email_basic(subject, snippet)

        # If regex is inconclusive, use AI
        if not classification:
            ai_result = await classify_email_ai(subject, snippet, from_addr)
            if ai_result and ai_result.get("confidence", 0) > 0.6:
                classification = ai_result.get("classification")
                company_hint = ai_result.get("company_name")
                job_hint = ai_result.get("job_title")
                summary = ai_result.get("summary", "")
            else:
                continue  # Can't classify, skip
        else:
            # For regex matches, try to extract company from sender/subject
            company_hint = _extract_company_from_email(from_addr, subject)
            job_hint = None
            summary = f"{classification}: {subject}"

        if classification == "irrelevant":
            continue

        # Try to match to an existing application
        if company_hint:
            match = match_email_to_application(db, profile_id, company_hint, job_hint)
            if match:
                job, app = match
                company = db.query(Company).filter(Company.id == job.company_id).first()
                changes = update_application_status(db, app, job, classification, summary)
                updates.append({
                    "email_id": email_id,
                    "classification": classification,
                    "company": company.name if company else company_hint,
                    "job_title": job.title,
                    "changes": changes,
                    "summary": summary,
                })
            else:
                updates.append({
                    "email_id": email_id,
                    "classification": classification,
                    "company": company_hint,
                    "job_title": job_hint,
                    "changes": None,
                    "summary": f"No matching application found for {company_hint}",
                })

    return {"updates": updates, "emails_processed": len(emails)}


def _extract_company_from_email(from_addr: str, subject: str) -> Optional[str]:
    """Try to extract company name from email sender or subject."""
    # From "CIRA <notifications@app.bamboohr.com>" extract "CIRA"
    name_match = re.match(r'^([^<]+)\s*<', from_addr)
    if name_match:
        name = name_match.group(1).strip()
        # Skip generic ATS names
        if name.lower() not in ("no-reply", "noreply", "notifications", "careers", "jobs", "hiring"):
            return name

    # Try subject: "Thank you for applying at CIRA"
    at_match = re.search(r"(?:at|for|from|with)\s+([A-Z][A-Za-z\s&]+?)(?:\s*[-–|!.]|\s*$)", subject)
    if at_match:
        return at_match.group(1).strip()

    return None


# ── Source Configuration Endpoints ────────────────────────────────────────

@router.get("/source-config")
async def get_source_config():
    """Return the configuration for each job source.

    Includes enabled status, search method, API key status (masked),
    health stats, and notes.
    """
    config = _get_source_config()
    health = get_source_health()

    result = {}
    for source_key, source_info in AVAILABLE_SOURCES.items():
        src_cfg = config.get(source_key, {})
        src_health = health.get(source_key, {})

        entry = {
            "name": source_info["name"],
            "enabled": src_cfg.get("enabled", True),
            "method": src_cfg.get("method", "scrape"),
            "notes": src_cfg.get("notes", ""),
            "region": source_info.get("region", "global"),
            "has_api_key": bool(src_cfg.get("api_key", "")),
            "has_app_id": bool(src_cfg.get("app_id", "")),
            "health": {
                "successes": src_health.get("successes", 0),
                "failures": src_health.get("failures", 0),
                "last_error": src_health.get("last_error", ""),
            },
        }
        result[source_key] = entry

    return result


@router.post("/source-config")
async def save_source_config(data: dict):
    """Save source configurations including API keys.

    Accepts a dict of source_key -> config object. Merges with existing config.
    """
    existing = _get_source_config()

    for source_key, new_cfg in data.items():
        if source_key not in AVAILABLE_SOURCES:
            continue
        if source_key not in existing:
            existing[source_key] = {}
        # Update fields selectively
        for field in ["enabled", "method", "api_key", "app_id", "notes"]:
            if field in new_cfg:
                existing[source_key][field] = new_cfg[field]

    _save_source_config(existing)
    return {"message": "Source configuration saved", "sources": list(data.keys())}


@router.get("/source-config/test")
async def test_source_config():
    """Run a quick test search against each enabled source.

    Returns status per source: ok, blocked, no_api_key, error,
    with result count and response time in ms.
    """
    import time as _time
    from backend.services.scraper import (
        search_linkedin_jobs, search_indeed, search_glassdoor,
        search_careerjet, search_jobbank, search_talent,
        search_adzuna, search_serpapi_google_jobs, search_remoteok,
        search_google_jobs, search_serpapi_indeed,
    )

    config = _get_source_config()

    source_fns = {
        "linkedin": search_linkedin_jobs,
        "indeed": search_indeed,
        "glassdoor": search_glassdoor,
        "careerjet": search_careerjet,
        "jobbank": search_jobbank,
        "talent": search_talent,
        "adzuna": search_adzuna,
        "serpapi": search_serpapi_google_jobs,
        "remoteok": search_remoteok,
        "google_jobs": search_google_jobs,
    }

    test_query = "software engineer"
    test_location = "Toronto"
    results = {}

    async def _test_one(source_key: str, fn):
        src_cfg = config.get(source_key, {})

        # Check if disabled
        if not src_cfg.get("enabled", True):
            return {"status": "disabled", "results": 0, "time_ms": 0}

        # Check API key requirements
        if source_key == "serpapi":
            key = os.environ.get("SERPAPI_KEY") or src_cfg.get("api_key", "")
            if not key:
                return {"status": "no_api_key", "results": 0, "time_ms": 0}
        elif source_key == "adzuna":
            key = os.environ.get("ADZUNA_APP_ID") or src_cfg.get("app_id", "")
            api_key = os.environ.get("ADZUNA_API_KEY") or src_cfg.get("api_key", "")
            if not key or not api_key:
                return {"status": "no_api_key", "results": 0, "time_ms": 0}

        # Check if Indeed should use SerpAPI
        if source_key == "indeed" and src_cfg.get("method") == "serpapi":
            serpapi_key = os.environ.get("SERPAPI_KEY") or config.get("serpapi", {}).get("api_key", "")
            if serpapi_key:
                fn = search_serpapi_indeed

        start = _time.monotonic()
        try:
            jobs = await asyncio.wait_for(fn(test_query, test_location, 2), timeout=30)
            elapsed = int((_time.monotonic() - start) * 1000)
            if jobs:
                return {"status": "ok", "results": len(jobs), "time_ms": elapsed}
            else:
                return {"status": "ok", "results": 0, "time_ms": elapsed, "note": "No results (may be normal)"}
        except asyncio.TimeoutError:
            elapsed = int((_time.monotonic() - start) * 1000)
            return {"status": "timeout", "results": 0, "time_ms": elapsed}
        except Exception as e:
            elapsed = int((_time.monotonic() - start) * 1000)
            err_str = str(e)[:200]
            if "403" in err_str or "blocked" in err_str.lower() or "cloudflare" in err_str.lower():
                return {"status": "blocked", "results": 0, "time_ms": elapsed, "error": err_str}
            return {"status": "error", "results": 0, "time_ms": elapsed, "error": err_str}

    # Run all tests concurrently
    tasks = {}
    for source_key, fn in source_fns.items():
        tasks[source_key] = _test_one(source_key, fn)

    test_results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    for source_key, result in zip(tasks.keys(), test_results):
        if isinstance(result, Exception):
            results[source_key] = {"status": "error", "results": 0, "time_ms": 0, "error": str(result)[:200]}
        else:
            results[source_key] = result

    # Summary
    working = sum(1 for r in results.values() if r.get("status") == "ok" and r.get("results", 0) > 0)
    total_results = sum(r.get("results", 0) for r in results.values())

    return {
        "summary": {
            "working_sources": working,
            "total_sources_tested": len(results),
            "total_test_results": total_results,
        },
        "sources": results,
    }
