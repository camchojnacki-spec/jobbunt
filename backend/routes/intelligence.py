"""AI-powered analysis and intelligence API routes."""
import asyncio
import json
import os
import re
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from backend.database import get_db, SessionLocal
from backend.models.models import Job, Application, Profile, Company, ProfileQuestion, User, Document, Interview, FollowUp
from backend.auth import get_optional_user
from backend.services.ai import ai_generate, ai_generate_json, ai_generate_stream, get_provider
from backend.services.enrichment import enrich_company
from backend.tasks import run_background, find_running_task
from backend.utils import safe_json, safe_json_list
from backend.serializers import _profile_dict, _job_dict
from backend.routes._helpers import _get_profile_for_user

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Career Stats (Baseball Card) ─────────────────────────────────────────

@router.post("/profiles/{profile_id}/career-stats")
async def extract_career_stats(profile_id: int, db: Session = Depends(get_db)):
    """Use AI to extract structured work history from resume for baseball card stat lines."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    # Return cached if available
    existing = safe_json(profile.career_history, [])
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
    roles = safe_json_list(profile.target_roles)
    skills = safe_json_list(profile.skills)
    career = safe_json(profile.career_history, [])
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


# ── Search Advisor ────────────────────────────────────────────────────

@router.get("/profiles/{profile_id}/search-advisor")
async def get_search_advisor(profile_id: int, db: Session = Depends(get_db)):
    """AI-powered career coach & search advisor."""
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
    target_roles = safe_json(profile.target_roles, [])
    profile_skills = safe_json(profile.skills, [])
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


def _build_advisor_prompt(profile_id: int, profile, db) -> str | None:
    """Build the search advisor prompt from profile + job data.

    Returns the prompt string, or None if insufficient data.
    """
    target_roles = safe_json(profile.target_roles, [])
    target_locations = safe_json(profile.target_locations, [])
    profile_skills = safe_json(profile.skills, [])

    if not target_roles and not profile_skills and not profile.resume_text:
        return None

    all_jobs = db.query(Job).filter(Job.profile_id == profile_id).all()

    liked = [j for j in all_jobs if j.status in ("liked", "shortlisted")]
    passed = [j for j in all_jobs if j.status == "passed"]
    high_score = [j for j in all_jobs if (j.match_score or 0) >= 65]
    low_score = [j for j in all_jobs if (j.match_score or 0) < 40]

    liked_summary = "\n".join([
        f"  - {j.title} at {j.company} (score: {j.match_score or 0:.0f}, seniority: {j.seniority_level or '?'})"
        for j in sorted(liked, key=lambda x: -(x.match_score or 0))[:10]
    ]) if liked else "None yet"

    passed_summary = "\n".join([
        f"  - {j.title} at {j.company} (score: {j.match_score or 0:.0f}, seniority: {j.seniority_level or '?'})"
        for j in sorted(passed, key=lambda x: -(x.match_score or 0))[:8]
    ]) if passed else "None yet"

    scores = [j.match_score for j in all_jobs if j.match_score]
    avg_score = sum(scores) / len(scores) if scores else 0

    seniority_dist = {}
    for j in all_jobs:
        lvl = (j.seniority_level or "unknown").lower()
        seniority_dist[lvl] = seniority_dist.get(lvl, 0) + 1

    job_skills_freq = {}
    for j in all_jobs:
        text = f"{j.description or ''} {j.requirements or ''}".lower()
        for skill in profile_skills:
            if skill.lower() in text:
                job_skills_freq[skill] = job_skills_freq.get(skill, 0) + 1

    high_score_skills_freq = {}
    for j in high_score:
        text = f"{j.description or ''} {j.requirements or ''}".lower()
        for skill in profile_skills:
            if skill.lower() in text:
                high_score_skills_freq[skill] = high_score_skills_freq.get(skill, 0) + 1

    high_score_titles = [j.title for j in high_score[:10]]
    low_score_titles = [j.title for j in low_score[:10]]
    high_score_companies = [j.company for j in high_score[:10]]

    answered_qs = db.query(ProfileQuestion).filter(
        ProfileQuestion.profile_id == profile_id,
        ProfileQuestion.is_answered == True,
    ).all()
    qa_context = "\n".join([f"Q: {q.question}\nA: {q.answer}" for q in answered_qs]) if answered_qs else "None yet"

    additional = safe_json(profile.additional_info, {})

    return f"""You are a senior executive career coach with deep expertise in career trajectory analysis, ATS optimization, and strategic job search planning.
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


async def _build_search_advisor(profile_id: int, profile, db):
    """Inner logic for search advisor, wrapped so caller can catch all errors."""
    prompt = _build_advisor_prompt(profile_id, profile, db)
    if prompt is None:
        return {"advisor": None, "reason": "Add target roles, skills, or upload a resume before requesting analysis"}

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
    except asyncio.TimeoutError:
        logger.warning("Search advisor generation timed out")
        advisor = None
        error_reason = "Analysis timed out — the AI service is slow. Please try again."
    except Exception as e:
        logger.warning(f"Search advisor generation failed: {e}", exc_info=True)
        advisor = None
        error_reason = f"AI analysis failed: {type(e).__name__}. Please try again."

    if advisor is None:
        return {"advisor": None, "reason": error_reason if 'error_reason' in dir() else "AI analysis could not be generated — try again in a moment"}

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


# ── Search Advisor (Streaming SSE) ───────────────────────────────────

@router.get("/profiles/{profile_id}/search-advisor-stream")
async def get_search_advisor_stream(profile_id: int, db: Session = Depends(get_db)):
    """Streaming variant of the search advisor — returns SSE chunks as AI generates."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    if get_provider() == "none":
        raise HTTPException(503, "No AI provider configured")

    prompt = _build_advisor_prompt(profile_id, profile, db)
    if prompt is None:
        raise HTTPException(400, "Add target roles, skills, or upload a resume before requesting analysis")

    async def event_stream():
        full_text = ""
        try:
            async for chunk in ai_generate_stream(prompt, max_tokens=4000, model_tier="deep"):
                if chunk:
                    full_text += chunk
                    # SSE format: data: <payload>\n\n
                    yield f"data: {json.dumps({'chunk': chunk})}\n\n"

            # Final event with the complete text for client-side JSON parsing
            yield f"data: {json.dumps({'done': True, 'full_text': full_text})}\n\n"

            # Post-processing: parse JSON and save advisor data (same as non-streaming)
            try:
                _save_advisor_results(profile_id, full_text)
            except Exception as e:
                logger.warning(f"Failed to save streaming advisor results: {e}")

        except Exception as e:
            logger.error(f"Search advisor stream error: {e}", exc_info=True)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _save_advisor_results(profile_id: int, full_text: str):
    """Parse the streamed AI text as JSON and save advisor data + questions to the DB."""
    import re as _re
    db = SessionLocal()
    try:
        # Parse JSON from the full text (same logic as ai_generate_json)
        cleaned = full_text.strip()
        fence_match = _re.search(r"```(?:json)?\s*\n([\s\S]*?)\n\s*```", cleaned)
        if fence_match:
            cleaned = fence_match.group(1).strip()
        else:
            cleaned = _re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
            cleaned = _re.sub(r"\n?```\s*$", "", cleaned)
            cleaned = cleaned.strip()

        advisor = None
        for attempt_text in [cleaned, full_text.strip()]:
            try:
                advisor = json.loads(attempt_text)
                break
            except (json.JSONDecodeError, ValueError):
                pass

        if advisor is None:
            json_match = _re.search(r"(\{[\s\S]*\})", cleaned)
            if json_match:
                try:
                    advisor = json.loads(json_match.group(1))
                except (json.JSONDecodeError, ValueError):
                    pass

        if not isinstance(advisor, dict) or "overall_assessment" not in advisor:
            return

        profile = db.query(Profile).filter(Profile.id == profile_id).first()
        if not profile:
            return

        # Cache advisor data on profile for scorer
        advisor_cache = {}
        for key in ("roles_to_consider", "keywords_for_ats", "industry_targets",
                     "companies_to_target", "skills_to_highlight", "skills_to_develop"):
            if advisor.get(key):
                advisor_cache[key] = advisor[key]
        if advisor_cache:
            profile.advisor_data = json.dumps(advisor_cache)
            db.commit()

        # Auto-create profile questions from advisor suggestions
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
        logger.warning(f"_save_advisor_results error: {e}")
        db.rollback()
    finally:
        db.close()


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
        "availability", "employment_type", "commute_tolerance",
        "relocation", "company_size", "industry_preference",
        "top_priority", "security_clearance", "travel_willingness",
        "additional_notes", "deal_breakers", "ideal_culture",
        "values", "strengths", "growth_areas",
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
    """Generate insights and trends across all jobs for the Summary tab."""
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


async def _build_insights(profile_id: int, profile, db):
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

    # Skills coverage analysis
    profile_skills = safe_json(profile.skills, [])
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
    except asyncio.TimeoutError:
        logger.warning("AI insights generation timed out")
        ai_insights = None
    except Exception as e:
        logger.warning(f"AI insights generation failed: {e}")
        ai_insights = None

    return {
        "ai_insights": ai_insights,
        "ai_error": "AI analysis timed out or failed — try again" if ai_insights is None else None
    }


# ── Apply Readiness ──────────────────────────────────────────────────────

@router.get("/profiles/{profile_id}/apply-readiness")
def get_apply_readiness(profile_id: int, db: Session = Depends(get_db)):
    """Check if profile has everything needed for auto-applications."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    profile_categories = []
    search_categories = []

    def add_profile_category(name, checks_list):
        passed = sum(1 for c in checks_list if c["passed"])
        profile_categories.append({
            "category": name,
            "checks": checks_list,
            "passed": passed,
            "total": len(checks_list),
        })

    def add_search_category(name, checks_list):
        passed = sum(1 for c in checks_list if c["passed"])
        search_categories.append({
            "category": name,
            "checks": checks_list,
            "passed": passed,
            "total": len(checks_list),
        })

    def check(name, passed, detail="", action=""):
        return {"name": name, "passed": passed, "detail": detail, "action": action}

    # ── Profile Basics (Profile Readiness) ─────────────────────────────
    basics = []
    basics.append(check("Full name", bool(profile.name), profile.name or "Missing", "scroll:f-name"))
    basics.append(check("Email address", bool(profile.email), profile.email or "Required for applications", "scroll:f-email"))
    basics.append(check("Phone number", bool(profile.phone), profile.phone or "Many applications require this", "scroll:f-phone"))
    basics.append(check("Location", bool(profile.location), profile.location or "Needed for location-based filtering", "scroll:f-location"))

    has_resume = bool(profile.resume_path) or bool(profile.resume_text)
    basics.append(check("Resume uploaded", has_resume, "Most applications require a resume file", "scroll:resume-drop-zone"))

    skills = safe_json(profile.skills, [])
    basics.append(check("Skills listed (3+)", len(skills) >= 3, f"{len(skills)} skills" if skills else "Add at least 3 skills", "scroll:f-skills-input"))

    basics.append(check("Years of experience", profile.experience_years is not None, f"{profile.experience_years} years" if profile.experience_years else "Helps with seniority matching", "scroll:f-experience"))

    roles = safe_json(profile.target_roles, [])
    basics.append(check("Target roles defined", len(roles) >= 1, f"{len(roles)} roles" if roles else "Define what you're looking for", "scroll:f-roles-input"))

    basics.append(check("Profile analyzed by AI", bool(profile.profile_analyzed), "Enables smarter cover letters and matching", "run:analyzeProfile"))

    basics.append(check("Cover letter style notes", bool(profile.cover_letter_template), "Optional but improves cover letter quality", "scroll:f-cover-template"))
    add_profile_category("Profile Basics", basics)

    # ── Profile Quality (Profile Readiness) ────────────────────────────
    quality = []
    summary = profile.profile_summary or ""
    quality.append(check("Profile summary", len(summary) > 50, f"{len(summary)} chars" if summary else "Run AI analysis to generate", "run:analyzeProfile"))

    quality.append(check("Career trajectory", bool(profile.career_trajectory), "Defined" if profile.career_trajectory else "Run AI analysis to generate", "run:analyzeProfile"))

    quality.append(check("Leadership style", bool(profile.leadership_style), "Defined" if profile.leadership_style else "Run AI analysis to generate", "run:analyzeProfile"))

    strengths = safe_json(profile.strengths, [])
    quality.append(check("Strengths identified (3+)", len(strengths) >= 3, f"{len(strengths)} strengths" if strengths else "Run AI analysis to generate", "run:analyzeProfile"))

    industries = safe_json(profile.industry_preferences, [])
    quality.append(check("Industry preference", len(industries) >= 1, f"{len(industries)} industries" if industries else "Run AI analysis or add manually", "scroll:reporter-question"))

    quality.append(check("Seniority level set", bool(profile.seniority_level), profile.seniority_level or "Run AI analysis to determine", "scroll:reporter-question"))
    add_profile_category("Profile Quality", quality)

    # ── Search Strategy (Search Performance) ───────────────────────────
    strategy = []

    job_count = db.query(Job).filter(Job.profile_id == profile_id).count()
    strategy.append(check("Performed a job search", job_count > 0, f"{job_count} jobs found" if job_count > 0 else "Search for jobs to start", "run:searchJobs"))

    strategy.append(check("AI advisor reviewed", bool(profile.profile_analyzed), "Analysis complete" if profile.profile_analyzed else "Get AI analysis from Summary > AI Advisor", "view:bullpen"))

    high_score_count = db.query(Job).filter(
        Job.profile_id == profile_id,
        Job.match_score >= 60
    ).count()
    strategy.append(check("5+ strong matches (score 60+)", high_score_count >= 5, f"{high_score_count} jobs above 60" if high_score_count > 0 else "Search and score more jobs", "run:searchJobs"))

    strategy.append(check("Salary expectations set", bool(profile.min_salary and profile.min_salary > 0), f"${profile.min_salary:,}+" if profile.min_salary else "Set minimum salary", "scroll:f-min-salary"))

    locations = safe_json(profile.target_locations, [])
    strategy.append(check("Location preferences set", len(locations) >= 1, f"{len(locations)} locations" if locations else "Add target locations", "scroll:f-locations-input"))

    strategy.append(check("Remote preference set", bool(profile.remote_preference), profile.remote_preference or "Set a preference", "scroll:f-remote"))
    add_search_category("Search Strategy", strategy)

    # ── Application Quality (Search Performance) ──────────────────────
    app_quality = []

    resume_recent = False
    resume_detail = "No resume uploaded"
    if has_resume and profile.updated_at:
        days_old = (datetime.utcnow() - profile.updated_at).days
        resume_recent = days_old <= 90
        resume_detail = f"Updated {days_old} days ago" if resume_recent else f"Last updated {days_old} days ago - consider refreshing"
    app_quality.append(check("Resume is recent (90 days)", resume_recent, resume_detail, "scroll:resume-drop-zone"))

    skills_in_demand = False
    skills_detail = "No skills or jobs to compare"
    if skills and job_count > 0:
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
    app_quality.append(check("Skills match market demand", skills_in_demand, skills_detail, "run:searchJobs"))

    roles_realistic = False
    roles_detail = "No target roles set"
    if roles and job_count > 0:
        role_match_count = 0
        for role in roles:
            # Check exact match first, then keyword match for multi-word roles
            matching = db.query(Job).filter(
                Job.profile_id == profile_id,
                Job.title.ilike(f"%{role}%")
            ).count()
            if matching == 0 and len(role.split()) >= 2:
                # Try matching key words from the role (e.g. "Security Architect" matches "IT Security Architect")
                keywords = [w for w in role.split() if len(w) > 3 and w.lower() not in ('senior', 'junior', 'lead', 'staff', 'principal')]
                if keywords:
                    from sqlalchemy import and_
                    keyword_filters = [Job.title.ilike(f"%{kw}%") for kw in keywords[:3]]
                    matching = db.query(Job).filter(
                        Job.profile_id == profile_id,
                        and_(*keyword_filters)
                    ).count()
            if matching > 0:
                role_match_count += 1
        # Also count high-scoring jobs as evidence of market relevance
        strong_matches = db.query(Job).filter(
            Job.profile_id == profile_id, Job.match_score >= 60
        ).count()
        roles_realistic = role_match_count > 0 or strong_matches >= 5
        if role_match_count > 0:
            roles_detail = f"{role_match_count}/{len(roles)} target roles found in listings"
        elif strong_matches >= 5:
            roles_detail = f"{strong_matches} strong matches (score 60+) found"
        else:
            roles_detail = "No matching jobs found - consider broadening"
    app_quality.append(check("Target roles match market", roles_realistic, roles_detail, "scroll:f-roles-input"))
    add_search_category("Application Quality", app_quality)

    # ── Aggregate ──────────────────────────────────────────────────────
    profile_checks = []
    for cat in profile_categories:
        profile_checks.extend(cat["checks"])

    search_checks = []
    for cat in search_categories:
        search_checks.extend(cat["checks"])

    profile_passed = sum(1 for c in profile_checks if c["passed"])
    profile_total = len(profile_checks)
    profile_score = round(profile_passed / profile_total * 100) if profile_total else 0

    search_passed = sum(1 for c in search_checks if c["passed"])
    search_total = len(search_checks)
    search_score = round(search_passed / search_total * 100) if search_total else 0

    return {
        "profile_score": profile_score,
        "profile_passed": profile_passed,
        "profile_total": profile_total,
        "search_score": search_score,
        "search_passed": search_passed,
        "search_total": search_total,
        "has_searched": job_count > 0,
        "ready": profile_score >= 70,
        "profile_categories": profile_categories,
        "search_categories": search_categories,
        # backward compat
        "score": profile_score,
        "passed": profile_passed + search_passed,
        "total": profile_total + search_total,
        "categories": profile_categories + search_categories,
    }


# ── Suggestions & Skills ─────────────────────────────────────────────────

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
            for chunk in re.split(r'[,;•·\n]', f"{j.requirements or ''}"):
                chunk = chunk.strip().strip('- •·').strip()
                if 2 <= len(chunk) <= 50 and not any(w in chunk.lower() for w in ['experience', 'years', 'ability to', 'must have', 'required', 'preferred', 'strong', 'excellent', 'proven']):
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
        existing = set(s.lower() for s in safe_json(profile.skills, []))
        suggestions = [
            {"value": k, "count": v}
            for k, v in sorted(skill_freq.items(), key=lambda x: -x[1])
            if k.lower() not in existing and (not q_lower or q_lower in k.lower())
        ][:25]
        return {"suggestions": suggestions}

    elif field == "roles":
        junk_titles = {"skip to filters", "browse", "best match", "sort by", "date posted",
                       "sign in", "log in", "search", "filter", "results", "next", "previous",
                       "view all", "load more", "show more", "apply now"}
        title_freq = {}
        for j in all_jobs:
            title = (j.title or "").strip()
            if (title and 5 <= len(title) < 80
                    and title.lower() not in junk_titles
                    and not any(p in title for p in ['CivicJobs.ca', 'CareerBeacon', 'Indeed.com', 'LinkedIn'])):
                title_freq[title] = title_freq.get(title, 0) + 1
        existing = set(r.lower() for r in safe_json(profile.target_roles, []))
        suggestions = [
            {"value": k, "count": v}
            for k, v in sorted(title_freq.items(), key=lambda x: -x[1])
            if k.lower() not in existing and (not q_lower or q_lower in k.lower())
        ][:25]
        return {"suggestions": suggestions}

    elif field == "locations":
        loc_freq = {}
        for j in all_jobs:
            loc = (j.location or "").strip()
            if loc and len(loc) < 80:
                loc_freq[loc] = loc_freq.get(loc, 0) + 1
        existing = set(l.lower() for l in safe_json(profile.target_locations, []))
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
    """Lightweight endpoint: return demand percentages for each profile skill."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    all_jobs = db.query(Job).filter(Job.profile_id == profile_id).all()
    if not all_jobs:
        return {"total_jobs": 0, "skill_hits": {}}

    profile_skills = safe_json(profile.skills, [])
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
    """Audit profile skills against actual job postings."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    all_jobs = db.query(Job).filter(Job.profile_id == profile_id).all()
    if not all_jobs:
        return {"audit": None, "reason": "No jobs found yet — search first"}

    profile_skills = safe_json(profile.skills, [])

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
    sample_reqs = "\n---\n".join([
        f"{j.title}: {(j.requirements or '')[:300]}"
        for j in sorted(all_jobs, key=lambda x: -(x.match_score or 0))[:20]
    ])

    ai_audit = None
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
            ai_audit = await asyncio.wait_for(
                ai_generate_json(prompt, max_tokens=1200, model_tier="balanced"),
                timeout=60.0
            )
        except asyncio.TimeoutError:
            logger.warning("Skills audit AI timed out, retrying with fast tier...")
            try:
                ai_audit = await asyncio.wait_for(
                    ai_generate_json(prompt, max_tokens=1200, model_tier="fast"),
                    timeout=45.0
                )
            except Exception:
                ai_audit = None
        except Exception as e:
            logger.warning(f"Skills audit AI failed: {e}")
            ai_audit = None

    return {
        "total_jobs": len(all_jobs),
        "jobs_analyzed_by_ai": min(len(all_jobs), 20),
        "data_source": "your_saved_jobs",
        "profile_skills": profile_skills,
        "skill_hits": skill_hits,
        "ai_audit": ai_audit,
    }


@router.get("/profiles/{profile_id}/skills-audit-stream")
async def skills_audit_stream(profile_id: int, db: Session = Depends(get_db)):
    """Streaming variant of skills audit — returns SSE chunks as AI generates.

    The non-streaming version times out after 60s. Streaming keeps the connection
    alive with progressive text, eliminating timeouts.
    """
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    if get_provider() == "none":
        raise HTTPException(503, "No AI provider configured")

    all_jobs = db.query(Job).filter(Job.profile_id == profile_id).all()
    if not all_jobs:
        raise HTTPException(400, "No jobs found yet — search first")

    profile_skills = safe_json(profile.skills, [])

    # Compute hit rate for each profile skill (fast, no AI needed)
    skill_hits = {}
    for skill in profile_skills:
        count = 0
        for j in all_jobs:
            text = f"{j.title or ''} {j.description or ''} {j.requirements or ''}".lower()
            if skill.lower() in text:
                count += 1
        skill_hits[skill] = {"count": count, "pct": round(count / len(all_jobs) * 100, 1)}

    # Build the AI prompt (same as non-streaming)
    sample_reqs = "\n---\n".join([
        f"{j.title}: {(j.requirements or '')[:300]}"
        for j in sorted(all_jobs, key=lambda x: -(x.match_score or 0))[:20]
    ])

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

    async def event_stream():
        full_text = ""
        try:
            # First send the non-AI data so frontend can render immediately
            yield f"data: {json.dumps({'meta': {'total_jobs': len(all_jobs), 'profile_skills': profile_skills, 'skill_hits': skill_hits}})}\n\n"

            async for chunk in ai_generate_stream(prompt, max_tokens=1200, model_tier="balanced"):
                if chunk:
                    full_text += chunk
                    yield f"data: {json.dumps({'chunk': chunk})}\n\n"

            yield f"data: {json.dumps({'done': True, 'full_text': full_text})}\n\n"

        except Exception as e:
            logger.error(f"Skills audit stream error: {e}", exc_info=True)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Stats ─────────────────────────────────────────────────────────────────

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
    at_bats = total
    hits = liked + applied + apps
    strikeouts = passed
    walks = shortlisted
    avg = round(hits / at_bats, 3) if at_bats > 0 else 0.0
    obp = round((hits + walks) / at_bats, 3) if at_bats > 0 else 0.0
    slg = round((applied * 2 + apps * 3) / at_bats, 3) if at_bats > 0 else 0.0
    ops = round(obp + slg, 3)

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
    """Process emails to find application status updates."""
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


# ── Resume Tailoring ─────────────────────────────────────────────────────

@router.post("/jobs/{job_id}/tailor-resume")
async def tailor_resume(job_id: int, db: Session = Depends(get_db)):
    """Generate a tailored resume optimized for a specific job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")

    profile = db.query(Profile).filter(Profile.id == job.profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    resume_text = profile.resume_text or profile.raw_profile_doc or ""
    if not resume_text:
        raise HTTPException(400, "No resume text found. Upload a resume first.")

    skills = safe_json_list(profile.skills)
    career_history = safe_json(profile.career_history, [])
    profile_summary = profile.profile_summary or ""

    prompt = f"""You are an expert resume writer. Tailor this resume for the specific job below.

ORIGINAL RESUME:
{resume_text}

CANDIDATE SKILLS: {json.dumps(skills)}
CAREER HISTORY: {json.dumps(career_history)}
CANDIDATE SUMMARY: {profile_summary}

TARGET JOB:
Title: {job.title}
Company: {job.company}
Description: {job.description or 'Not available'}
Requirements: {job.requirements or 'Not specified'}

Instructions:
1. Rewrite the professional summary to align with this specific role
2. Reorder experience sections to highlight the most relevant roles first
3. Rewrite bullet points to emphasize skills and achievements that match the job requirements
4. Ensure all relevant skills from the candidate's profile that match the job are prominently featured
5. Remove or de-emphasize experience that is not relevant to this role
6. Keep the resume truthful — do not fabricate experience, only reframe and emphasize existing experience
7. Maintain professional formatting with clear sections

Return ONLY the tailored resume text, no commentary."""

    content = await ai_generate(prompt, max_tokens=3000, model_tier="deep")
    if not content:
        raise HTTPException(500, "AI generation failed. Try again.")

    # Determine version number
    existing_count = db.query(Document).filter(
        Document.profile_id == profile.id,
        Document.job_id == job.id,
        Document.doc_type == "tailored_resume",
    ).count()

    doc = Document(
        profile_id=profile.id,
        job_id=job.id,
        doc_type="tailored_resume",
        version=existing_count + 1,
        title=f"Tailored Resume - {job.title} at {job.company}",
        content=content,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    return {
        "tailored_resume": content,
        "document_id": doc.id,
        "changes_summary": f"Resume tailored for {job.title} at {job.company} (v{doc.version})",
    }


# ── Interview Prep (Warm-Up) ────────────────────────────────────────────

@router.post("/jobs/{job_id}/interview-prep")
async def interview_prep(job_id: int, db: Session = Depends(get_db)):
    """Generate interview preparation materials for a specific job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")

    profile = db.query(Profile).filter(Profile.id == job.profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    # Gather company data
    company_name = job.company or "Unknown"
    culture_insights = ""
    if job.company_id:
        company = db.query(Company).filter(Company.id == job.company_id).first()
        if company:
            culture_insights = company.culture_summary or ""

    # Also pull job-level culture insights from deep research
    if not culture_insights and job.culture_insights:
        culture_insights = job.culture_insights

    # Profile data
    profile_summary = profile.profile_summary or ""
    skills = safe_json_list(profile.skills)
    strengths = safe_json_list(profile.strengths)
    career_history = safe_json(profile.career_history, [])
    red_flags = safe_json_list(job.red_flags)
    match_reasons = safe_json_list(job.match_reasons)

    prompt = f"""You are an expert interview coach. Generate interview preparation materials for this candidate and job.

CANDIDATE PROFILE:
Summary: {profile_summary}
Skills: {json.dumps(skills)}
Strengths: {json.dumps(strengths)}
Career History: {json.dumps(career_history)}

TARGET JOB:
Title: {job.title} at {company_name}
Description: {job.description or 'Not available'}
Requirements: {job.requirements or 'Not specified'}
Company Culture: {culture_insights or 'Unknown'}
Match Reasons: {json.dumps(match_reasons)}
Red Flags: {json.dumps(red_flags)}

Generate a JSON response with this structure:
{{
  "behavioral_questions": [
    {{
      "question": "Tell me about a time when...",
      "why_asked": "They want to assess...",
      "star_framework": {{
        "situation": "Draw from your experience at [specific company/role from career history]...",
        "task": "Your responsibility was...",
        "action": "Emphasize how you...",
        "result": "Quantify the outcome..."
      }}
    }}
  ],
  "technical_questions": [
    {{
      "question": "How would you approach...",
      "talking_points": ["Point 1 drawing from their skills", "Point 2", "Point 3"]
    }}
  ],
  "questions_to_ask": [
    {{
      "question": "What does success look like in the first 90 days?",
      "why_good": "Shows you're thinking about impact and alignment"
    }}
  ],
  "key_talking_points": ["Point about relevant experience", "Point about matching skill"],
  "preparation_tips": ["Research the company's recent...", "Review your experience with..."]
}}

Generate exactly 5 behavioral questions with STAR frameworks personalized to the candidate's career history.
Generate exactly 5 technical/role-specific questions with answer talking points.
Generate exactly 3 questions the candidate should ask the interviewer.
Generate 4-6 key talking points that connect the candidate's experience to this role.
Generate 3-5 preparation tips.

Return ONLY valid JSON, no other text."""

    result = await ai_generate_json(prompt, max_tokens=4000, model_tier="deep")
    if not result:
        raise HTTPException(500, "AI generation failed. Try again.")

    # Save prep content to user_notes (append, don't overwrite)
    prep_summary = f"\n\n--- Interview Prep (generated) ---\nBehavioral Qs: {len(result.get('behavioral_questions', []))}, Technical Qs: {len(result.get('technical_questions', []))}"
    existing_notes = job.user_notes or ""
    if "--- Interview Prep (generated) ---" not in existing_notes:
        job.user_notes = (existing_notes + prep_summary).strip()

    # If application exists, also save to an Interview record
    application = db.query(Application).filter(
        Application.job_id == job.id,
        Application.profile_id == profile.id,
    ).first()
    if application:
        interview = Interview(
            application_id=application.id,
            profile_id=profile.id,
            interview_type="behavioral",
            prep_notes=json.dumps(result),
            outcome="pending",
        )
        db.add(interview)

    db.commit()

    return result


# ── Follow-Up Reminders ──────────────────────────────────────────────────

@router.get("/profiles/{profile_id}/follow-ups")
async def get_follow_ups(profile_id: int, db: Session = Depends(get_db)):
    """Return pending follow-ups, auto-creating them for stale applications."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    now = datetime.utcnow()
    stale_applied_cutoff = now - timedelta(days=7)
    stale_interview_cutoff = now - timedelta(days=3)

    # Find stale 'applied' applications (>7 days old)
    stale_applied = db.query(Application).filter(
        Application.profile_id == profile_id,
        Application.pipeline_status == "applied",
        Application.created_at < stale_applied_cutoff,
    ).all()

    # Find stale 'interview' applications (no update in 3+ days)
    stale_interviews = db.query(Application).filter(
        Application.profile_id == profile_id,
        Application.pipeline_status == "interview",
        Application.updated_at < stale_interview_cutoff,
    ).all()

    stale_apps = stale_applied + stale_interviews
    created_count = 0

    for app in stale_apps:
        # Check if a pending follow-up already exists
        existing_fu = db.query(FollowUp).filter(
            FollowUp.application_id == app.id,
            FollowUp.completed == False,
        ).first()
        if not existing_fu:
            fu = FollowUp(
                application_id=app.id,
                profile_id=profile_id,
                follow_up_type="status_check",
                due_date=now,
            )
            db.add(fu)
            created_count += 1

    if created_count > 0:
        db.commit()

    # Fetch all pending follow-ups
    pending = db.query(FollowUp).filter(
        FollowUp.profile_id == profile_id,
        FollowUp.completed == False,
    ).all()

    follow_ups = []
    for fu in pending:
        app = db.query(Application).filter(Application.id == fu.application_id).first() if fu.application_id else None
        job = app.job if app else None
        days_stale = (now - (app.updated_at or app.created_at)).days if app else 0

        # Generate a brief draft if not already present
        if not fu.draft_content and job:
            try:
                prompt = f"""Write a brief, professional follow-up message (2-3 sentences) for a job application.
Job title: {job.title}
Company: {job.company}
Applied {days_stale} days ago.
Status: {app.pipeline_status if app else 'unknown'}

Keep it concise and professional. Just the message body, no subject line."""
                draft = await ai_generate(prompt, max_tokens=200, model_tier="flash")
                if draft:
                    fu.draft_content = draft.strip()
            except Exception as e:
                logger.warning(f"Failed to generate follow-up draft: {e}")

        follow_ups.append({
            "id": fu.id,
            "application_id": fu.application_id,
            "job_title": job.title if job else "Unknown",
            "company": job.company if job else "Unknown",
            "days_stale": days_stale,
            "follow_up_type": fu.follow_up_type,
            "due_date": fu.due_date.isoformat() if fu.due_date else None,
            "draft_content": fu.draft_content,
            "pipeline_status": app.pipeline_status if app else None,
        })

    if created_count > 0:
        db.commit()  # Save any generated drafts

    return {"follow_ups": follow_ups, "auto_created": created_count}


@router.post("/follow-ups/{follow_up_id}/complete")
async def complete_follow_up(follow_up_id: int, db: Session = Depends(get_db)):
    """Mark a follow-up as completed."""
    fu = db.query(FollowUp).filter(FollowUp.id == follow_up_id).first()
    if not fu:
        raise HTTPException(404, "Follow-up not found")
    fu.completed = True
    fu.completed_at = datetime.utcnow()
    db.commit()
    return {"status": "completed", "id": fu.id}


@router.post("/follow-ups/{follow_up_id}/draft-email")
async def draft_follow_up_email(follow_up_id: int, db: Session = Depends(get_db)):
    """Generate a detailed follow-up email using AI."""
    fu = db.query(FollowUp).filter(FollowUp.id == follow_up_id).first()
    if not fu:
        raise HTTPException(404, "Follow-up not found")

    app = db.query(Application).filter(Application.id == fu.application_id).first() if fu.application_id else None
    job = app.job if app else None
    profile = db.query(Profile).filter(Profile.id == fu.profile_id).first()
    days_stale = (datetime.utcnow() - (app.updated_at or app.created_at)).days if app else 0

    candidate_name = profile.name if profile else "the candidate"
    prompt = f"""Write a professional follow-up email for a job application.

Candidate: {candidate_name}
Job title: {job.title if job else 'Unknown'}
Company: {job.company if job else 'Unknown'}
Applied {days_stale} days ago
Current status: {app.pipeline_status if app else 'unknown'}
Follow-up type: {fu.follow_up_type}

Write a complete, professional email with:
- A clear subject line (on its own line prefixed with "Subject: ")
- A warm but professional greeting
- A brief reminder of the application
- Expression of continued interest
- A polite request for an update
- Professional sign-off using the candidate's name

Keep it concise (under 200 words) and avoid being pushy."""

    draft = await ai_generate(prompt, max_tokens=500, model_tier="flash")
    if not draft:
        raise HTTPException(500, "AI generation failed. Try again.")

    fu.draft_content = draft.strip()
    db.commit()

    return {"id": fu.id, "draft_content": fu.draft_content}


# ── Box Score Analytics ──────────────────────────────────────────────────

@router.get("/profiles/{profile_id}/box-score")
async def get_box_score(profile_id: int, db: Session = Depends(get_db)):
    """Return comprehensive analytics for the profile's job search."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    now = datetime.utcnow()
    seven_days_ago = now - timedelta(days=7)

    jobs = db.query(Job).filter(Job.profile_id == profile_id).all()
    applications = db.query(Application).filter(Application.profile_id == profile_id).all()

    total_scouted = len(jobs)
    total_shortlisted = sum(1 for j in jobs if j.status in ("liked", "shortlisted"))
    total_applied = len(applications)
    total_interviews = sum(1 for a in applications if a.pipeline_status in ("interview", "screening"))
    total_offers = sum(1 for a in applications if a.pipeline_status == "offer")

    batting_avg = total_offers / total_applied if total_applied > 0 else 0
    responded = sum(1 for a in applications if a.pipeline_status not in ("applied", "no_response"))
    response_rate = responded / total_applied if total_applied > 0 else 0

    scores = [j.match_score for j in jobs if j.match_score is not None and j.match_score > 0]
    avg_score = sum(scores) / len(scores) if scores else 0

    # Source breakdown — use sources_seen for consistency across views (B15 fix)
    source_breakdown = {}
    for j in jobs:
        try:
            srcs = json.loads(j.sources_seen or "[]") if j.sources_seen else []
        except (json.JSONDecodeError, TypeError):
            srcs = []
        if not srcs:
            srcs = [j.source or "unknown"]
        for src in srcs:
            source_breakdown[src] = source_breakdown.get(src, 0) + 1

    # Score distribution
    buckets = {"0-20": 0, "21-40": 0, "41-60": 0, "61-80": 0, "81-100": 0}
    for s in scores:
        if s <= 20:
            buckets["0-20"] += 1
        elif s <= 40:
            buckets["21-40"] += 1
        elif s <= 60:
            buckets["41-60"] += 1
        elif s <= 80:
            buckets["61-80"] += 1
        else:
            buckets["81-100"] += 1

    recent_activity = sum(1 for j in jobs if j.created_at and j.created_at >= seven_days_ago)

    return {
        "total_scouted": total_scouted,
        "total_shortlisted": total_shortlisted,
        "total_applied": total_applied,
        "total_interviews": total_interviews,
        "total_offers": total_offers,
        "batting_avg": round(batting_avg, 4),
        "response_rate": round(response_rate, 4),
        "avg_score": round(avg_score, 1),
        "source_breakdown": source_breakdown,
        "score_distribution": buckets,
        "recent_activity": recent_activity,
    }


# ── Game Summary (Weekly AI Recap) ──────────────────────────────────────

@router.get("/profiles/{profile_id}/game-summary")
async def get_game_summary(profile_id: int, db: Session = Depends(get_db)):
    """Generate an AI-powered weekly summary of job search activity in baseball commentary style."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    now = datetime.utcnow()
    seven_days_ago = now - timedelta(days=7)

    # Gather weekly stats
    jobs = db.query(Job).filter(Job.profile_id == profile_id).all()
    applications = db.query(Application).filter(Application.profile_id == profile_id).all()

    week_jobs = [j for j in jobs if j.created_at and j.created_at >= seven_days_ago]
    week_apps = [a for a in applications if a.created_at and a.created_at >= seven_days_ago]
    week_shortlisted = [j for j in week_jobs if j.status in ("liked", "shortlisted")]
    week_interviews = [a for a in week_apps if a.pipeline_status in ("interview", "screening")]

    # Check for AI tools usage (documents generated this week)
    week_docs = db.query(Document).filter(
        Document.profile_id == profile_id,
        Document.created_at >= seven_days_ago
    ).count()
    week_interview_preps = db.query(Interview).filter(
        Interview.profile_id == profile_id,
        Interview.created_at >= seven_days_ago
    ).count()

    # All-time stats for context
    total_applied = len(applications)
    total_offers = sum(1 for a in applications if a.pipeline_status == "offer")
    batting_avg = total_offers / total_applied if total_applied > 0 else 0

    # Top companies applied to this week
    week_companies = []
    for a in week_apps:
        job = next((j for j in jobs if j.id == a.job_id), None)
        if job and job.company:
            week_companies.append(job.company)

    # Top-scored jobs this week
    top_jobs = sorted(week_jobs, key=lambda j: j.match_score or 0, reverse=True)[:5]
    top_job_lines = [f"  - {j.title} at {j.company} (score: {j.match_score or 0:.0f})" for j in top_jobs]

    stats = {
        "new_jobs_found": len(week_jobs),
        "jobs_shortlisted": len(week_shortlisted),
        "applications_submitted": len(week_apps),
        "interviews_scheduled": len(week_interviews),
        "ai_tools_used": week_docs + week_interview_preps,
        "total_applied_alltime": total_applied,
        "total_offers_alltime": total_offers,
        "batting_avg": round(batting_avg, 4),
    }

    player_name = profile.name or "the player"

    prompt = f"""You are a veteran baseball radio announcer giving the weekly game summary for a job seeker's search campaign.
The job seeker's name is {player_name}.

Here are this week's stats (past 7 days):
- New jobs scouted: {stats['new_jobs_found']}
- Jobs shortlisted (moved to lineup): {stats['jobs_shortlisted']}
- Applications submitted (at-bats): {stats['applications_submitted']}
- Interviews scheduled: {stats['interviews_scheduled']}
- AI tools used (cover letters, interview prep, resume tailoring): {stats['ai_tools_used']}
- Season batting average (offers / applications): .{int(stats['batting_avg'] * 1000):03d}
- Companies swung at this week: {', '.join(week_companies[:8]) if week_companies else 'None yet'}
- Top prospects scouted:
{chr(10).join(top_job_lines) if top_job_lines else '  (No new prospects this week)'}

Write a 3-4 paragraph game summary in the style of a colorful baseball radio announcer calling the week's highlights.
- Use baseball metaphors throughout (at-bats, batting practice, bullpen, lineup, scouting report, etc.)
- Reference specific numbers from the stats
- Be encouraging but honest — if activity was low, motivate them to step up to the plate
- End with 2-3 specific, actionable pieces of advice for next week framed as coaching tips
- Keep it fun, energetic, and under 350 words
- Do NOT use markdown formatting — plain text only"""

    provider = get_provider()
    if provider == "none":
        summary_text = (
            f"This week's recap: {stats['new_jobs_found']} new jobs scouted, "
            f"{stats['jobs_shortlisted']} shortlisted, {stats['applications_submitted']} applications sent. "
            "AI summary unavailable — no AI provider configured."
        )
    else:
        summary_text = await ai_generate(prompt, max_tokens=800, model_tier="balanced", use_cache=False)
        if not summary_text:
            summary_text = (
                f"Couldn't generate the play-by-play this time. "
                f"Quick stats: {stats['new_jobs_found']} scouted, {stats['applications_submitted']} applied, "
                f"{stats['interviews_scheduled']} interviews on deck."
            )

    return {
        "summary": summary_text,
        "stats": stats,
        "week_start": seven_days_ago.isoformat(),
        "week_end": now.isoformat(),
    }


# ── Achievements / Badges ────────────────────────────────────────────────

ACHIEVEMENTS = [
    {
        "id": "first_hit",
        "name": "First Hit",
        "emoji": "\u26be",
        "hint": "Submit your first application",
    },
    {
        "id": "on_base",
        "name": "On Base",
        "emoji": "\U0001f7e2",
        "hint": "Shortlist 5 jobs",
    },
    {
        "id": "rbi",
        "name": "RBI",
        "emoji": "\U0001f3c3",
        "hint": "Schedule your first interview",
    },
    {
        "id": "home_run",
        "name": "Home Run",
        "emoji": "\U0001f3c6",
        "hint": "Receive a job offer",
    },
    {
        "id": "grand_slam",
        "name": "Grand Slam",
        "emoji": "\U0001f4a5",
        "hint": "Receive 4+ offers",
    },
    {
        "id": "perfect_game",
        "name": "Perfect Game",
        "emoji": "\U0001f947",
        "hint": "100% profile completeness",
    },
    {
        "id": "triple_play",
        "name": "Triple Play",
        "emoji": "\U0001f52e",
        "hint": "Use 3+ AI tools",
    },
    {
        "id": "iron_man",
        "name": "Iron Man",
        "emoji": "\U0001f4aa",
        "hint": "7 consecutive days of activity",
    },
    {
        "id": "slugger",
        "name": "Slugger",
        "emoji": "\U0001f3cf",
        "hint": "Submit 20+ applications",
    },
    {
        "id": "all_star",
        "name": "All-Star",
        "emoji": "\u2b50",
        "hint": "Match score 80+ on any job",
    },
    {
        "id": "gold_glove",
        "name": "Gold Glove",
        "emoji": "\U0001f9e4",
        "hint": "Have your resume analyzed by AI",
    },
    {
        "id": "mvp",
        "name": "MVP",
        "emoji": "\U0001f451",
        "hint": "Complete all Spring Training levels",
    },
]


@router.get("/profiles/{profile_id}/achievements")
def get_achievements(profile_id: int, db: Session = Depends(get_db)):
    """Check various milestones and return unlocked achievement badges."""
    from sqlalchemy import func as sa_func

    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")

    applications = db.query(Application).filter(Application.profile_id == profile_id).all()
    jobs = db.query(Job).filter(Job.profile_id == profile_id).all()

    app_count = len(applications)
    shortlisted_count = sum(1 for j in jobs if j.status in ("liked", "shortlisted"))
    interview_count = sum(1 for a in applications if a.pipeline_status in ("interview", "screening"))
    offer_count = sum(1 for a in applications if a.pipeline_status == "offer")
    has_high_match = any(j.match_score and j.match_score >= 80 for j in jobs)

    # Profile completeness (reuse apply-readiness logic inline)
    profile_fields = [
        profile.name, profile.email, profile.phone, profile.location,
        profile.resume_text or profile.resume_path,
        profile.target_roles, profile.target_locations,
        profile.seniority_level, profile.min_salary,
        profile.remote_preference, profile.industry_preference,
    ]
    profile_filled = sum(1 for f in profile_fields if f)
    profile_score = round(profile_filled / len(profile_fields) * 100)

    # AI tools usage: check for tailored resume docs, interview prep (interviews table), skills audit cache
    ai_tools_used = 0
    tailored = db.query(Document).filter(
        Document.profile_id == profile_id, Document.doc_type == "tailored_resume"
    ).first()
    if tailored:
        ai_tools_used += 1

    interview_records = db.query(Interview).filter(Interview.profile_id == profile_id).first()
    if interview_records:
        ai_tools_used += 1

    # Skills audit: check if advisor_data is populated (set by skills-audit / search-advisor)
    if profile.advisor_data:
        ai_tools_used += 1

    # Resume analyzed by AI
    resume_analyzed = profile.profile_analyzed or False

    # Iron Man: 7 consecutive days of activity (jobs created or applications)
    iron_man = False
    activity_dates = set()
    for j in jobs:
        if j.created_at:
            activity_dates.add(j.created_at.date())
    for a in applications:
        if a.created_at:
            activity_dates.add(a.created_at.date())
        if a.updated_at:
            activity_dates.add(a.updated_at.date())

    if len(activity_dates) >= 7:
        sorted_dates = sorted(activity_dates)
        streak = 1
        for i in range(1, len(sorted_dates)):
            if (sorted_dates[i] - sorted_dates[i - 1]).days == 1:
                streak += 1
                if streak >= 7:
                    iron_man = True
                    break
            else:
                streak = 1

    # Spring Training: check if all levels complete
    has_resume = bool(profile.resume_path) or bool(profile.resume_text)
    target_roles = safe_json(profile.target_roles, [])
    target_locations = safe_json(profile.target_locations, [])
    has_basic_fields = bool(profile.name and profile.email and profile.location
                           and len(target_roles) > 0 and len(target_locations) > 0)
    has_deep_analysis = bool(profile.seniority_level and (profile.min_salary or profile.availability))
    deal_breakers = safe_json(profile.deal_breakers, [])
    has_reporter = bool(profile.remote_preference and profile.industry_preference and len(deal_breakers) > 0)
    spring_training_complete = has_resume and has_basic_fields and has_deep_analysis and has_reporter

    # Build results
    unlocked = {
        "first_hit": app_count >= 1,
        "on_base": shortlisted_count >= 5,
        "rbi": interview_count >= 1,
        "home_run": offer_count >= 1,
        "grand_slam": offer_count >= 4,
        "perfect_game": profile_score >= 100,
        "triple_play": ai_tools_used >= 3,
        "iron_man": iron_man,
        "slugger": app_count >= 20,
        "all_star": has_high_match,
        "gold_glove": resume_analyzed,
        "mvp": spring_training_complete,
    }

    badges = []
    for ach in ACHIEVEMENTS:
        badges.append({
            **ach,
            "unlocked": unlocked.get(ach["id"], False),
        })

    earned = sum(1 for b in badges if b["unlocked"])
    return {
        "badges": badges,
        "earned": earned,
        "total": len(badges),
    }
