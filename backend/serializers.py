"""Serialization functions for Jobbunt API responses."""
import json

from backend.models.models import Job, Application, Profile, AgentQuestion, Company
from backend.services.enrichment import company_dict
from backend.utils import safe_json, safe_json_list


def _preload_companies(db, jobs) -> dict:
    """Batch-load Company objects for a list of jobs, returning {id: Company}."""
    company_ids = {j.company_id for j in jobs if j.company_id}
    if not company_ids:
        return {}
    companies = db.query(Company).filter(Company.id.in_(company_ids)).all()
    return {c.id: c for c in companies}


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


def _profile_dict(p: Profile) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "has_pin": bool(p.pin),
        "email": p.email,
        "phone": p.phone,
        "location": p.location,
        "target_roles": safe_json_list(p.target_roles),
        "target_locations": safe_json_list(p.target_locations),
        "min_salary": p.min_salary,
        "max_salary": p.max_salary,
        "remote_preference": p.remote_preference,
        "experience_years": p.experience_years,
        "skills": safe_json_list(p.skills),
        "resume_uploaded": bool(p.resume_path),
        "has_resume_text": bool(p.resume_text),
        "cover_letter_template": p.cover_letter_template,
        "has_profile_doc": bool(p.raw_profile_doc),
        # Deep profile insights
        "profile_analyzed": p.profile_analyzed or False,
        "profile_summary": p.profile_summary,
        "career_trajectory": p.career_trajectory,
        "leadership_style": p.leadership_style,
        "industry_preferences": safe_json_list(p.industry_preferences),
        "values": safe_json_list(p.values),
        "deal_breakers": safe_json_list(p.deal_breakers),
        "strengths": safe_json_list(p.strengths),
        "growth_areas": safe_json_list(p.growth_areas),
        "ideal_culture": p.ideal_culture,
        "seniority_level": p.seniority_level,
        "search_tiers_down": p.search_tiers_down or 0,
        "search_tiers_up": p.search_tiers_up or 0,
        "career_history": safe_json(p.career_history, []),
        # Reporter Corner fields
        "availability": p.availability,
        "employment_type": p.employment_type,
        "commute_tolerance": p.commute_tolerance,
        "relocation": p.relocation,
        "company_size": p.company_size,
        "industry_preference": p.industry_preference,
        "top_priority": p.top_priority,
        "security_clearance": p.security_clearance,
        "travel_willingness": p.travel_willingness,
        "additional_notes": p.additional_notes,
    }


def _job_dict(j: Job, db=None, company_map: dict | None = None) -> dict:
    """Serialize a Job to dict.

    Args:
        db: DB session for lazy company lookup (single-job endpoints).
        company_map: Pre-loaded {company_id: Company} dict to avoid N+1 queries
                     in list endpoints.  Takes priority over ``db`` lookup.
    """
    company_data = None
    if j.company_id:
        if company_map is not None:
            company = company_map.get(j.company_id)
            if company:
                company_data = company_dict(company)
        elif db:
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
        # User notes
        "user_notes": getattr(j, 'user_notes', None) or "",
    }


def _application_dict(a: Application, db=None) -> dict:
    return {
        "id": a.id,
        "job_id": a.job_id,
        "job_title": a.job.title if a.job else "",
        "company": a.job.company if a.job else "",
        "status": a.status,
        "pipeline_status": getattr(a, 'pipeline_status', None) or "applied",
        "cover_letter": a.cover_letter,
        "agent_log": json.loads(a.agent_log or "[]"),
        "error_message": a.error_message,
        "notes": getattr(a, 'notes', None) or "",
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
