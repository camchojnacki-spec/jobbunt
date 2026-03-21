"""Job-related API routes."""
import asyncio
import json
import re
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel

from backend.database import get_db, SessionLocal
from backend.models.models import Job, Application, Profile, AgentQuestion, Company, User
from backend.auth import get_optional_user
from backend.services.scraper import search_all_sources, save_scraped_jobs
from backend.services.scorer import score_job_multidim, score_and_update_job, score_and_update_job_ai
from backend.services.agent import start_application, process_application
from backend.services.ai import ai_generate, ai_generate_json
from backend.services.enrichment import enrich_job, analyze_profile
from backend.services.browser_scraper import build_search_urls, get_extractor, DETAIL_EXTRACTOR, SCROLL_AND_LOAD, PAGINATION_URLS
from backend.tasks import run_background, find_running_task, is_task_cancelled
from backend.utils import safe_json
from backend.serializers import _job_dict, _application_dict, _job_completeness, _preload_companies
from backend.routes._helpers import (
    _get_profile_for_user, _safe_enrich, _safe_score, _rescore_progress,
)

try:
    from backend.services.dispatch import scrape_indeed
except ImportError:
    scrape_indeed = None

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Pydantic schemas ──────────────────────────────────────────────────────

class SwipeAction(BaseModel):
    action: str  # "like", "pass", or "shortlist"

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

class DispatchJob(BaseModel):
    title: str
    company: str = "Unknown"
    location: str = ""
    description: str = ""
    url: str = ""
    salary_text: str = ""
    source: str = "indeed"

class DispatchPayload(BaseModel):
    jobs: list[DispatchJob]


# ── Search & Import ───────────────────────────────────────────────────────

@router.post("/profiles/{profile_id}/search")
async def search_jobs(
    profile_id: int,
    sources: Optional[list[str]] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_optional_user),
):
    """Trigger a job search for a profile.

    Launches the search as a background task and returns immediately with a task_id.
    The frontend polls /tasks/{task_id} for completion and /profiles/{id}/jobs/recent
    for incremental results.

    Args:
        sources: Optional list of source keys to search (e.g. ["linkedin", "indeed", "gcjobs"]).
                 If omitted, auto-detects based on target locations.
    """
    profile = _get_profile_for_user(profile_id, user, db)

    # Don't start a new search if one is already running
    existing = find_running_task("search", profile_id)
    if existing:
        return {"task_id": existing, "status": "running"}

    # Deep-analyze profile if not done yet (uses rich pasted doc)
    if not profile.profile_analyzed and (profile.raw_profile_doc or profile.resume_text):
        try:
            await analyze_profile(db, profile)
        except Exception as e:
            logger.warning(f"Profile analysis failed: {e}")

    task_id = run_background("search", profile_id, _bg_search_and_score, profile_id, sources)
    return {"task_id": task_id, "status": "running"}


async def _bg_search_and_score(profile_id: int, sources: list[str] | None, task_id: str = None):
    """Background worker: scrape, save, rule-score, then kick off enrichment."""
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.id == profile_id).first()
        if not profile:
            return {"error": "Profile not found"}

        # Check for cancellation before starting scrape
        if task_id and is_task_cancelled(task_id):
            return {"error": "Cancelled by user", "new_jobs": 0}

        # 1. SCRAPE
        search_result = await search_all_sources(profile, sources=sources)
        raw_jobs = search_result["jobs"]
        relevance_filtered = search_result.get("relevance_filtered", 0)
        recommended_sources = search_result.get("recommended_sources", [])

        # Check for cancellation after scrape
        if task_id and is_task_cancelled(task_id):
            return {"error": "Cancelled by user", "new_jobs": 0}

        # 2. DEDUPLICATE & SAVE
        new_jobs = save_scraped_jobs(db, profile_id, raw_jobs)

        # 3. QUICK RULE-BASED SCORING
        cmap = _preload_companies(db, new_jobs)
        for idx, job in enumerate(new_jobs):
            try:
                company = cmap.get(job.company_id) if job.company_id else None
                result = score_job_multidim(job, profile, company)
                job.match_score = result["score"]
                job.match_reasons = json.dumps(result["reasons"])
                job.match_breakdown = json.dumps(result["breakdown"])
                if (idx + 1) % 10 == 0:
                    db.commit()
                    # Check for cancellation periodically during scoring
                    if task_id and is_task_cancelled(task_id):
                        db.commit()
                        return {"error": "Cancelled by user", "new_jobs": len(new_jobs)}
            except Exception:
                pass
        db.commit()

        # 4. ENRICH & AI-SCORE (background sub-task — runs independently)
        job_ids = [j.id for j in new_jobs]
        if job_ids:
            asyncio.get_event_loop().create_task(
                _bg_enrich_and_score(profile_id, job_ids)
            )

        return {
            "total_found": len(raw_jobs) + relevance_filtered,
            "new_jobs": len(new_jobs),
            "duplicates_skipped": len(raw_jobs) - len(new_jobs),
            "relevance_filtered": relevance_filtered,
            "recommended_sources": recommended_sources,
        }
    except Exception as e:
        logger.error(f"Background search failed: {e}")
        return {"error": str(e)}
    finally:
        db.close()


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


@router.get("/profiles/{profile_id}/jobs/recent")
def get_recent_jobs(profile_id: int, since: Optional[str] = None,
                    db: Session = Depends(get_db), user: User = Depends(get_optional_user)):
    """Get jobs added since a timestamp. Used by frontend polling during search."""
    _get_profile_for_user(profile_id, user, db)
    query = db.query(Job).filter(Job.profile_id == profile_id, Job.status == "pending")
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace('Z', '+00:00'))
            query = query.filter(Job.created_at > since_dt)
        except Exception:
            pass
    jobs = query.order_by(Job.match_score.desc()).limit(50).all()
    cmap = _preload_companies(db, jobs)
    return {
        "jobs": [_job_dict(j, company_map=cmap) for j in jobs],
        "count": len(jobs),
        "total_pending": db.query(Job).filter(Job.profile_id == profile_id, Job.status == "pending").count()
    }


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
    cmap = _preload_companies(db, new_jobs)
    for idx, job in enumerate(new_jobs):
        try:
            company = cmap.get(job.company_id) if job.company_id else None
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
    """Get search URLs and JS extractors for browser-based scraping."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    target_roles = safe_json(profile.target_roles, [])
    target_locations = safe_json(profile.target_locations, [])

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


# ── Job List & Swipe ─────────────────────────────────────────────────────

@router.get("/profiles/{profile_id}/jobs")
def list_jobs(
    profile_id: int,
    status: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    min_score: float = Query(0),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_optional_user),
):
    """List jobs for a profile, optionally filtered by status and source.

    Supports pagination via ``limit`` (default 100, max 500) and ``offset``.
    Response includes ``total`` count so the frontend can render paging controls.
    """
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

    total = query.count()
    jobs = query.order_by(Job.match_score.desc()).offset(offset).limit(limit).all()
    cmap = _preload_companies(db, jobs)
    return {
        "jobs": [_job_dict(j, company_map=cmap) for j in jobs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


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
        ready_jobs.append(j)

    ready_jobs = ready_jobs[:limit]
    cmap = _preload_companies(db, ready_jobs)
    return [_job_dict(j, company_map=cmap) for j in ready_jobs]


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


# ── Cover Letter Generation ──────────────────────────────────────────────

@router.post("/jobs/{job_id}/generate-cover-letter")
async def generate_cover_letter(job_id: int, db: Session = Depends(get_db)):
    """Generate a tailored cover letter for a job based on the user's profile."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")

    profile = db.query(Profile).filter(Profile.id == job.profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    company = db.query(Company).filter(Company.id == job.company_id).first() if job.company_id else None

    # Build the prompt
    profile_section = f"""Candidate Profile:
Name: {profile.name or 'N/A'}
Location: {profile.location or 'N/A'}
Experience: {profile.experience_years or 'N/A'} years
Skills: {profile.skills or 'N/A'}
Resume excerpt: {(profile.resume_text or '')[:3000]}
{f'Career trajectory: {profile.career_trajectory}' if getattr(profile, 'career_trajectory', None) else ''}
{f'Leadership style: {profile.leadership_style}' if getattr(profile, 'leadership_style', None) else ''}
{f'Cover letter style preferences: {profile.cover_letter_template}' if profile.cover_letter_template else ''}"""

    job_section = f"""Job Details:
Title: {job.title}
Company: {job.company}
Location: {job.location or 'N/A'}
{f'Salary: {job.salary_text}' if job.salary_text else ''}
Description: {(job.description or '')[:3000]}"""

    company_section = ""
    if company:
        company_section = f"""
Company Intel:
{f'Industry: {company.industry}' if getattr(company, 'industry', None) else ''}
{f'Size: {company.size}' if getattr(company, 'size', None) else ''}
{f'Culture: {company.culture_notes}' if getattr(company, 'culture_notes', None) else ''}"""

    prompt = f"""{profile_section}

{job_section}
{company_section}

Write a compelling, professional cover letter for this candidate applying to this specific job.

Requirements:
- 3-4 paragraphs, concise and impactful
- Open with a strong hook — not "I am writing to express my interest"
- Highlight 2-3 specific skills/experiences that match the job requirements
- Show knowledge of the company if intel is available
- Close with enthusiasm and a clear call to action
- Professional but personable tone
- Do NOT include placeholder brackets like [Company Name] — use the actual details
- Do NOT include the date, addresses, or "Dear Hiring Manager" header — just the body text
- Keep it under 400 words"""

    try:
        cover_letter = await ai_generate(prompt, max_tokens=1500, model_tier="balanced")
        return {"cover_letter": cover_letter.strip(), "job_id": job_id}
    except Exception as e:
        logger.error(f"Cover letter generation failed for job {job_id}: {e}")
        raise HTTPException(500, f"Cover letter generation failed: {str(e)}")


# ── Job Detail & Enrichment ──────────────────────────────────────────────

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
    """Verify all unverified pending jobs for a profile."""
    from backend.services.enrichment import verify_job_active

    jobs = (
        db.query(Job)
        .filter(
            Job.profile_id == profile_id,
            Job.status == "pending",
            Job.url_valid.is_(None),
            Job.url.isnot(None),
            Job.url != "",
        )
        .limit(20)
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


@router.put("/jobs/{job_id}/notes")
async def update_job_notes(job_id: int, data: dict = Body(...), db: Session = Depends(get_db)):
    """Update user notes on a job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    if "user_notes" in data:
        job.user_notes = data["user_notes"]
    db.commit()
    return {"status": "updated", "user_notes": job.user_notes}


# ── Rescore ──────────────────────────────────────────────────────────────

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
    """Background rescore task — uses its own DB session.

    Parallelizes AI scoring with a semaphore (max 5 concurrent) for throughput.
    Falls back to rule-based scoring if AI fails for individual jobs.
    """
    from backend.database import SessionLocal
    db = SessionLocal()
    sem = asyncio.Semaphore(5)
    scored_counter = {"n": 0}  # mutable counter shared across tasks

    try:
        profile = db.query(Profile).filter(Profile.id == profile_id).first()
        if not profile:
            _rescore_progress[profile_id] = {"current": 0, "total": 0, "status": "error"}
            return

        jobs = db.query(Job).filter(Job.profile_id == profile_id).all()
        total = len(jobs)
        cmap = _preload_companies(db, jobs)

        async def _score_one(job):
            async with sem:
                company = cmap.get(job.company_id) if job.company_id else None
                try:
                    await score_and_update_job_ai(db, job, profile, company)
                except Exception:
                    try:
                        score_and_update_job(db, job, profile, company)
                    except Exception:
                        pass
                scored_counter["n"] += 1
                _rescore_progress[profile_id] = {
                    "current": scored_counter["n"], "total": total, "status": "running",
                }

        await asyncio.gather(
            *[_score_one(job) for job in jobs],
            return_exceptions=True,
        )

        _rescore_progress[profile_id] = {"current": scored_counter["n"], "total": total, "status": "done"}
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


# ── Dedup & Cleanup ──────────────────────────────────────────────────────

@router.post("/profiles/{profile_id}/reconcile-duplicates")
async def reconcile_duplicates(profile_id: int, db: Session = Depends(get_db)):
    """AI-driven duplicate reconciliation."""
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
    cmap = _preload_companies(db, jobs)
    for job in jobs:
        company = cmap.get(job.company_id) if job.company_id else None
        try:
            await _deep_research(db, job, profile, company)
            researched += 1
        except Exception as e:
            logger.warning(f"Deep research failed for job {job.id}: {e}")

    return {"researched": researched, "total": len(jobs)}


# ── Shortlist ─────────────────────────────────────────────────────────

@router.get("/profiles/{profile_id}/shortlist")
def get_shortlist(
    profile_id: int,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Get shortlisted jobs for a profile (paginated)."""
    base = db.query(Job).filter(Job.profile_id == profile_id, Job.status == "shortlisted")
    total = base.count()
    jobs = base.order_by(Job.match_score.desc()).offset(offset).limit(limit).all()
    cmap = _preload_companies(db, jobs)
    return {
        "jobs": [_job_dict(j, company_map=cmap) for j in jobs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


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


# ── Dispatch Scout ─────────────────────────────────────────────────────

@router.post("/profiles/{profile_id}/dispatch")
async def ingest_dispatched_jobs(
    profile_id: int,
    payload: DispatchPayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_optional_user),
):
    """Ingest jobs from an external dispatch agent (browser automation, Claude in Chrome, etc)."""
    profile = _get_profile_for_user(profile_id, user, db)
    if not payload.jobs:
        return {"ingested": 0, "message": "No jobs provided"}

    raw_jobs = []
    for j in payload.jobs:
        raw_jobs.append({
            "title": j.title,
            "company": j.company,
            "location": j.location,
            "description": j.description,
            "url": j.url,
            "source": j.source,
            "sources_seen": [j.source],
            "salary_text": j.salary_text,
        })

    new_jobs = save_scraped_jobs(db, profile_id, raw_jobs)

    # Score new jobs in background
    if new_jobs:
        background_tasks.add_task(_bg_score_dispatched, profile_id, [j.id for j in new_jobs])

    return {
        "ingested": len(new_jobs),
        "duplicates_skipped": len(raw_jobs) - len(new_jobs),
        "message": f"Dispatched {len(new_jobs)} new jobs from {payload.jobs[0].source if payload.jobs else 'unknown'}",
    }


async def _bg_score_dispatched(profile_id: int, job_ids: list[int]):
    """Background: rule-score + enrich dispatched jobs."""
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.id == profile_id).first()
        if not profile:
            return
        for jid in job_ids:
            job = db.query(Job).filter(Job.id == jid).first()
            if not job:
                continue
            try:
                score_and_update_job(db, job, profile)
            except Exception as e:
                logger.warning(f"Dispatch scoring failed for job {jid}: {e}")
        # Kick off AI enrichment for new jobs
        for jid in job_ids[:20]:  # Cap AI calls
            job = db.query(Job).filter(Job.id == jid).first()
            if job and not job.company_obj:
                try:
                    await enrich_job(db, job, profile)
                except Exception as e:
                    logger.debug(f"Dispatch enrichment failed for job {jid}: {e}")
    finally:
        db.close()


@router.get("/profiles/{profile_id}/dispatch-config")
def get_dispatch_config(
    profile_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_optional_user),
):
    """Return search parameters for an external dispatch agent to use."""
    profile = _get_profile_for_user(profile_id, user, db)

    target_roles = []
    if profile.target_roles:
        try:
            target_roles = json.loads(profile.target_roles) if isinstance(profile.target_roles, str) else profile.target_roles
        except (json.JSONDecodeError, TypeError):
            target_roles = [profile.target_roles]

    locations = []
    if profile.target_locations:
        try:
            locations = json.loads(profile.target_locations) if isinstance(profile.target_locations, str) else profile.target_locations
        except (json.JSONDecodeError, TypeError):
            locations = [profile.target_locations]
    if not locations and profile.location:
        locations = [profile.location]

    # Build search URLs for Indeed
    searches = []
    for role in (target_roles or [""])[:5]:
        for loc in (locations or [""])[:3]:
            q = role.strip()
            l = loc.strip()
            indeed_url = f"https://www.indeed.com/jobs?q={q}&l={l}" if l else f"https://www.indeed.com/jobs?q={q}"
            indeed_ca_url = f"https://ca.indeed.com/jobs?q={q}&l={l}" if l else f"https://ca.indeed.com/jobs?q={q}"
            searches.append({
                "query": q,
                "location": l,
                "indeed_url": indeed_url,
                "indeed_ca_url": indeed_ca_url,
            })

    return {
        "profile_id": profile_id,
        "searches": searches,
        "post_endpoint": f"/api/profiles/{profile_id}/dispatch",
        "instructions": (
            "For each search URL, navigate to the page, extract job cards "
            "(title, company, location, URL, salary if visible, snippet/description), "
            "then POST them as JSON to the dispatch endpoint. "
            "Format: {\"jobs\": [{\"title\": ..., \"company\": ..., \"location\": ..., "
            "\"url\": ..., \"description\": ..., \"salary_text\": ..., \"source\": \"indeed\"}]}"
        ),
    }


@router.post("/profiles/{profile_id}/dispatch-run")
async def run_dispatch_scout(
    profile_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_optional_user),
):
    """Launch automated Indeed scraping via Playwright with the user's Chrome profile."""
    if scrape_indeed is None:
        raise HTTPException(501, "Dispatch requires Playwright — only available on local dev, not Cloud Run")

    profile = _get_profile_for_user(profile_id, user, db)

    # Check if already running
    existing = find_running_task("dispatch", profile_id)
    if existing:
        return {"task_id": existing, "status": "running"}

    # Build search config
    config = get_dispatch_config(profile_id, db=db, user=user)
    searches = config["searches"]

    if not searches:
        return {"error": "No target roles or locations configured", "status": "error"}

    task_id = run_background("dispatch", profile_id, _bg_dispatch, profile_id, searches)
    return {"task_id": task_id, "status": "running"}


async def _bg_dispatch(profile_id: int, searches: list[dict]):
    """Background: run Playwright Indeed scrape, save & score results."""
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.id == profile_id).first()
        if not profile:
            return {"error": "Profile not found"}

        # Run the scraper (opens Chrome window)
        raw_jobs = await scrape_indeed(searches, max_pages=2)
        if not raw_jobs:
            return {"ingested": 0, "message": "No jobs found — Indeed may have blocked the session"}

        # Save & dedup
        new_jobs = save_scraped_jobs(db, profile_id, raw_jobs)

        # Score new jobs
        for job in new_jobs:
            try:
                score_and_update_job(db, job, profile)
            except Exception as e:
                logger.warning(f"Dispatch scoring failed for job {job.id}: {e}")

        # Enrich top jobs
        for job in sorted(new_jobs, key=lambda j: j.match_score or 0, reverse=True)[:15]:
            if not job.company_obj:
                try:
                    await enrich_job(db, job, profile)
                except Exception:
                    pass

        return {
            "ingested": len(new_jobs),
            "duplicates_skipped": len(raw_jobs) - len(new_jobs),
            "total_scraped": len(raw_jobs),
        }
    finally:
        db.close()
