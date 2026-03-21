"""API routes for Jobbunt — thin aggregator that includes sub-routers."""
import asyncio
import json
import os
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.models import Company, User
from backend.auth import get_optional_user
from backend.services.scraper import _get_source_config, _save_source_config, get_source_health, AVAILABLE_SOURCES
from backend.services.enrichment import enrich_company, company_dict
from backend.services.ai import ai_generate_json, get_provider
from backend.tasks import get_task_status, cancel_task

from backend.routes.profiles import router as profiles_router
from backend.routes.jobs import router as jobs_router
from backend.routes.applications import router as applications_router
from backend.routes.intelligence import router as intelligence_router

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# Include sub-routers
router.include_router(profiles_router)
router.include_router(jobs_router)
router.include_router(applications_router)
router.include_router(intelligence_router)


# ── Background Task Status ────────────────────────────────────────────────

@router.get("/tasks/{task_id}")
async def poll_task_status(task_id: str):
    """Poll a background task for its status and result."""
    task = get_task_status(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.post("/tasks/{task_id}/cancel")
async def cancel_task_endpoint(task_id: str):
    """Cancel a running background task."""
    task = cancel_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


# ── Sources & Config ──────────────────────────────────────────────────────

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


# ── Company endpoints ────────────────────────────────────────────────────

@router.post("/profiles/{profile_id}/reenrich-companies")
async def reenrich_companies(profile_id: int, db: Session = Depends(get_db)):
    """Re-enrich all companies missing website domains (for logo support)."""
    from backend.models.models import Job, Profile
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


# ── AI Provider ──────────────────────────────────────────────────────────

@router.get("/ai-provider")
def get_ai_provider():
    """Check which AI provider is active."""
    provider = get_provider()
    return {"provider": provider}


# ── Prompt Lab & Model Configuration ─────────────────────────────────────

@router.get("/config/prompts")
def get_all_prompts_api():
    """Return all AI prompts grouped by category."""
    from backend.services.prompt_registry import get_all_prompts
    return get_all_prompts()


@router.get("/config/prompts/{key}")
def get_prompt_api(key: str):
    """Return a single prompt's metadata and template."""
    from backend.services.prompt_registry import get_prompt
    prompt = get_prompt(key)
    if not prompt:
        raise HTTPException(404, f"Prompt '{key}' not found")
    return prompt


@router.put("/config/prompts/{key}")
def update_prompt_api(key: str, data: dict):
    """Update a prompt template at runtime."""
    from backend.services.prompt_registry import update_prompt
    template = data.get("prompt_template", "")
    if not template:
        raise HTTPException(400, "prompt_template is required")
    if update_prompt(key, template):
        return {"status": "updated", "key": key}
    raise HTTPException(404, f"Prompt '{key}' not found")


@router.post("/config/prompts/{key}/reset")
def reset_prompt_api(key: str):
    """Reset a prompt to its default template."""
    from backend.services.prompt_registry import reset_prompt, get_default_template
    was_modified = reset_prompt(key)
    default = get_default_template(key)
    if default is None:
        raise HTTPException(404, f"Prompt '{key}' not found")
    return {"status": "reset", "was_modified": was_modified, "prompt_template": default}


@router.post("/config/prompts/{key}/enhance")
async def enhance_prompt_api(key: str):
    """Use AI to suggest improvements to a prompt template."""
    from backend.services.prompt_registry import get_prompt
    prompt_data = get_prompt(key)
    if not prompt_data:
        raise HTTPException(404, f"Prompt '{key}' not found")

    current_template = prompt_data["prompt_template"]
    name = prompt_data["name"]
    description = prompt_data["description"]

    enhance_prompt = f"""You are an expert prompt engineer. Analyze this AI prompt template and suggest improvements.

PROMPT NAME: {name}
DESCRIPTION: {description}
MODEL TIER: {prompt_data['model_tier']}

CURRENT PROMPT TEMPLATE:
---
{current_template}
---

Analyze the prompt and provide:
1. A quality score (0-100) for the current prompt
2. Specific suggestions for improvement
3. An improved version of the prompt

Return JSON:
{{
    "quality_score": <0-100>,
    "analysis": "2-3 sentences analyzing the current prompt's strengths and weaknesses",
    "suggestions": ["specific suggestion 1", "suggestion 2", "suggestion 3"],
    "improved_template": "The full improved prompt template (keep the same variable placeholders like {{variable_name}})"
}}

Focus on: clarity, specificity, output format consistency, guardrails, and scoring calibration."""

    result = await ai_generate_json(enhance_prompt, max_tokens=2000, model_tier="balanced")
    if not result:
        raise HTTPException(500, "AI enhancement failed — try again")
    # Ensure required fields exist with sensible defaults
    result.setdefault("quality_score", 50)
    result.setdefault("analysis", "Analysis unavailable.")
    result.setdefault("suggestions", [])
    result.setdefault("improved_template", current_template)
    # Coerce quality_score to int in case AI returned a string
    try:
        result["quality_score"] = int(result["quality_score"])
    except (ValueError, TypeError):
        result["quality_score"] = 50
    return result


@router.get("/config/models")
def get_models_api():
    """Return available models and current configuration."""
    from backend.services.prompt_registry import get_model_config
    return get_model_config()


@router.put("/config/models/override")
def set_model_override_api(data: dict):
    """Set a per-feature model tier override."""
    from backend.services.prompt_registry import set_model_override, clear_model_override
    feature_key = data.get("feature_key", "")
    model_tier = data.get("model_tier", "")

    if not feature_key:
        raise HTTPException(400, "feature_key is required")

    # Allow clearing override by passing empty/null tier
    if not model_tier:
        clear_model_override(feature_key)
        return {"status": "cleared", "feature_key": feature_key}

    if set_model_override(feature_key, model_tier):
        return {"status": "updated", "feature_key": feature_key, "model_tier": model_tier}
    raise HTTPException(400, f"Invalid feature_key or model_tier")


# ── Source Configuration Endpoints ────────────────────────────────────────

@router.get("/source-config")
async def get_source_config():
    """Return the configuration for each job source."""
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
    """Save source configurations including API keys."""
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
    """Run a quick test search against each enabled source."""
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
