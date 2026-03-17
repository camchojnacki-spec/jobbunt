"""Application agent - uses AI to generate cover letters and manage the application process.

Supports two modes:
1. AI-assisted preparation: Generates cover letter, analyzes requirements, identifies questions
2. Browser-ready: Prepares all data needed for browser-based form filling (via MCP Chrome tools)
"""
import json
import logging
import os
import datetime
from typing import Optional

from sqlalchemy.orm import Session

from backend.models.models import Job, Application, Profile, AgentQuestion
from backend.services.ai import ai_generate, ai_generate_json, get_provider

logger = logging.getLogger(__name__)


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


# ── Application Steps ─────────────────────────────────────────────────────
# Each application goes through these steps, tracked in agent_log

STEPS = {
    "verify": "Verify job is still active",
    "analyze": "Analyze application requirements",
    "prepare": "Prepare application materials",
    "questions": "Answer required questions",
    "ready": "Ready for submission",
    "submitted": "Application submitted",
}


def _log_step(log: list, step: str, status: str, details: str = "", **extra) -> list:
    """Append a step to the agent log."""
    entry = {
        "step": step,
        "status": status,
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "details": details,
    }
    entry.update(extra)
    log.append(entry)
    return log


async def generate_cover_letter(profile: Profile, job: Job) -> str:
    """Generate a tailored cover letter using AI (Anthropic or Gemini)."""
    if get_provider() == "none":
        return _fallback_cover_letter(profile, job)

    # Build rich context from profile doc if available
    profile_context = ""
    if profile.raw_profile_doc:
        profile_context = f"\nFull profile document:\n{profile.raw_profile_doc[:3000]}"

    # Pull in additional Q&A context from profile interviews
    additional_answers = ""
    try:
        additional = _safe_json(profile.additional_info, {})
        if additional:
            additional_answers = "\n".join([f"- {k}: {v}" for k, v in list(additional.items())[:10]])
    except Exception:
        pass

    prompt = f"""Write a compelling, detailed cover letter for this candidate applying to this job.

STRUCTURE (4-5 substantive paragraphs):
1. Opening hook — why this specific role at this specific company excites them. Reference something concrete about the company.
2. Core experience — 2-3 specific accomplishments with metrics/outcomes that directly relate to this role's key requirements.
3. Leadership & strategic value — how their leadership style, strategic thinking, or unique perspective would add value beyond the job description.
4. Cultural & mission alignment — why they'd thrive at this company specifically, referencing company culture, mission, or values if known.
5. Strong close — confident but not arrogant. Express genuine enthusiasm and a clear call to action.

RULES:
- Be SPECIFIC: name actual technologies, methodologies, industries, outcomes
- Include METRICS where possible (team sizes, budgets, % improvements, revenue impact)
- Sound like a real human who is genuinely excited — not a template
- Mirror the company's language/tone from the job description
- 400-600 words total
- Do NOT use phrases like "I am writing to express my interest" or "I believe I would be a great fit"

CANDIDATE:
Name: {profile.name}
Skills: {profile.skills}
Experience: {profile.experience_years} years
Resume excerpt: {(profile.resume_text or '')[:2500]}
{f'Cover letter style preferences: {profile.cover_letter_template}' if profile.cover_letter_template else ''}
{f'Career trajectory: {profile.career_trajectory}' if profile.career_trajectory else ''}
{f'Leadership style: {profile.leadership_style}' if profile.leadership_style else ''}
{f'Key strengths: {profile.strengths}' if profile.strengths else ''}
{f'Additional context from candidate interviews:{chr(10)}{additional_answers}' if additional_answers else ''}
{profile_context}

JOB:
Title: {job.title}
Company: {job.company}
Location: {job.location or 'Not specified'}
{f'Role summary: {job.role_summary}' if job.role_summary else ''}
Description: {(job.description or '')[:3000]}

Write the complete cover letter including "Dear Hiring Manager," salutation and closing with the candidate's name.
"""
    result = await ai_generate(prompt, max_tokens=1500, model_tier="balanced")
    return result.strip() if result else _fallback_cover_letter(profile, job)


def _fallback_cover_letter(profile: Profile, job: Job) -> str:
    skills = _safe_json(profile.skills, [])
    skills_str = ", ".join(skills[:5]) if skills else "relevant skills"
    return (
        f"I am writing to express my strong interest in the {job.title} position at {job.company}. "
        f"With {profile.experience_years or 'several'} years of experience and expertise in {skills_str}, "
        f"I am confident in my ability to contribute meaningfully to your team.\n\n"
        f"I would welcome the opportunity to discuss how my background aligns with your needs."
    )


async def start_application(db: Session, job: Job, profile: Profile) -> Application:
    """Start the application process for a job."""
    # Check if already applied
    existing = db.query(Application).filter(
        Application.job_id == job.id,
        Application.profile_id == profile.id,
        Application.status.in_(["completed", "in_progress", "ready"]),
    ).first()

    if existing:
        return existing

    log = []
    _log_step(log, "verify", "started", "Checking if job is still active")

    # Step 1: Verify job is still active
    if job.url:
        from backend.services.enrichment import verify_job_active
        result = await verify_job_active(db, job)
        if not result.get("verified"):
            _log_step(log, "verify", "failed", "Job posting appears expired or removed")
            application = Application(
                job_id=job.id,
                profile_id=profile.id,
                status="failed",
                error_message="Job posting is no longer active",
                agent_log=json.dumps(log),
            )
            db.add(application)
            db.commit()
            db.refresh(application)
            return application
        _log_step(log, "verify", "completed", "Job confirmed active")
    else:
        _log_step(log, "verify", "skipped", "No URL to verify")

    # Step 2: Generate cover letter
    _log_step(log, "prepare", "started", "Generating cover letter")
    cover_letter = await generate_cover_letter(profile, job)
    _log_step(log, "prepare", "completed", "Cover letter generated")

    application = Application(
        job_id=job.id,
        profile_id=profile.id,
        status="in_progress",
        cover_letter=cover_letter,
        agent_log=json.dumps(log),
    )
    db.add(application)
    job.status = "applied"
    db.commit()
    db.refresh(application)

    return application


async def process_application(db: Session, application: Application) -> dict:
    """Process an application - analyze requirements and prepare for submission.

    Returns a status dict with application details and any questions needed.
    """
    job = application.job
    profile = application.profile
    log = json.loads(application.agent_log or "[]")

    # Check if the job URL is available for application
    if not job.url:
        _log_step(log, "analyze", "failed", "No application URL available")
        application.status = "failed"
        application.error_message = "No application URL available"
        application.agent_log = json.dumps(log)
        db.commit()
        return {"status": "failed", "error": "No application URL"}

    # Step 3: Analyze requirements
    _log_step(log, "analyze", "started", "Analyzing application requirements")
    analysis = await _analyze_application(job, profile)
    _log_step(log, "analyze", "completed",
              f"Strategy: {analysis.get('application_strategy', 'Direct apply')}",
              application_type=analysis.get("application_type", "unknown"),
              platform=analysis.get("platform", "unknown"))

    # If there are questions, create them and pause
    questions = analysis.get("questions_for_candidate", [])
    if questions:
        for q_text in questions:
            q = AgentQuestion(
                application_id=application.id,
                job_id=job.id,
                question=q_text,
                context=f"Applying to {job.title} at {job.company}",
            )
            db.add(q)

        _log_step(log, "questions", "waiting", f"{len(questions)} questions for candidate")
        application.status = "needs_input"
        application.agent_log = json.dumps(log)
        db.commit()

        return {
            "status": "needs_input",
            "questions": questions,
            "application_id": application.id,
        }

    # Step 4: Package application for submission
    app_package = _build_application_package(profile, job, application, analysis)

    _log_step(log, "ready", "completed",
              f"Application prepared for {analysis.get('platform', 'direct')} submission",
              package=app_package)

    application.status = "ready"
    application.agent_log = json.dumps(log)
    db.commit()

    return {
        "status": "ready",
        "application_id": application.id,
        "strategy": analysis.get("application_strategy", ""),
        "platform": analysis.get("platform", "unknown"),
        "application_type": analysis.get("application_type", "unknown"),
        "package": app_package,
    }


async def _analyze_application(job: Job, profile: Profile) -> dict:
    """Use AI to analyze what's needed to apply."""
    analysis = {
        "can_proceed": True,
        "missing_info": [],
        "questions_for_candidate": [],
        "application_strategy": "Direct apply via job URL",
        "application_type": "external",
        "platform": _detect_platform(job.url or ""),
    }

    if get_provider() == "none":
        return analysis

    additional_info = _safe_json(profile.additional_info, {})
    prompt = f"""You are a job application assistant. Analyze this job posting and determine:
1. What information is needed to apply (beyond standard resume/cover letter)
2. Whether there are any questions that need the candidate's input
3. What platform/method should be used to apply
4. Whether this is an Easy Apply, direct company site, or external ATS

Job: {job.title} at {job.company}
URL: {job.url}
Description: {(job.description or '')[:3000]}
{f'Role summary: {job.role_summary}' if job.role_summary else ''}

Candidate info we already have:
- Name: {profile.name}
- Email: {profile.email}
- Phone: {profile.phone}
- Skills: {profile.skills}
- Location: {profile.location}
- Experience: {profile.experience_years} years
- Additional answers: {json.dumps(additional_info)}

Respond in JSON:
{{
    "can_proceed": true/false,
    "missing_info": ["list of info we still need"],
    "questions_for_candidate": ["specific questions to ask - only ask what we absolutely cannot infer"],
    "application_strategy": "step-by-step description of how to apply",
    "application_type": "easy_apply|company_site|ats|email|unknown",
    "platform": "linkedin|indeed|glassdoor|workday|greenhouse|lever|company_direct|other",
    "form_fields_expected": ["name", "email", "resume", "cover_letter", "other fields"],
    "notes": "any other observations"
}}

IMPORTANT: Only ask questions that we genuinely cannot answer from the profile. Do NOT ask about:
- Name, email, phone (we have those)
- Skills/experience (we have those)
- Availability to work (assume yes)
- Authorization to work (only ask if explicitly required in posting)"""

    result = await ai_generate_json(prompt, max_tokens=1000, model_tier="balanced")
    if result:
        analysis.update(result)
    return analysis


def _detect_platform(url: str) -> str:
    """Detect the application platform from the URL."""
    url_lower = url.lower()
    platforms = {
        "linkedin.com": "linkedin",
        "indeed.com": "indeed",
        "glassdoor.com": "glassdoor",
        "myworkdayjobs.com": "workday",
        "workday.com": "workday",
        "greenhouse.io": "greenhouse",
        "lever.co": "lever",
        "icims.com": "icims",
        "taleo": "taleo",
        "smartrecruiters": "smartrecruiters",
        "jobvite.com": "jobvite",
        "usajobs.gov": "usajobs",
        "careers.": "company_direct",
        "jobs.": "company_direct",
    }
    for pattern, platform in platforms.items():
        if pattern in url_lower:
            return platform
    return "other"


def _build_application_package(profile: Profile, job: Job, application: Application, analysis: dict) -> dict:
    """Build the data package needed for browser-based form submission."""
    skills = _safe_json(profile.skills, [])
    additional = _safe_json(profile.additional_info, {})

    package = {
        "url": job.url,
        "platform": analysis.get("platform", "unknown"),
        "application_type": analysis.get("application_type", "unknown"),
        "candidate": {
            "name": profile.name,
            "email": profile.email,
            "phone": profile.phone,
            "location": profile.location,
            "experience_years": profile.experience_years,
            "skills": skills,
            "resume_path": profile.resume_path,
            "has_resume": bool(profile.resume_path),
        },
        "materials": {
            "cover_letter": application.cover_letter,
        },
        "job_context": {
            "title": job.title,
            "company": job.company,
            "location": job.location,
        },
        "form_fields": analysis.get("form_fields_expected", []),
        "strategy": analysis.get("application_strategy", ""),
        "additional_answers": additional,
    }

    return package


async def submit_application(db: Session, application_id: int) -> dict:
    """Mark an application as submitted (called after browser-based submission)."""
    application = db.query(Application).filter(Application.id == application_id).first()
    if not application:
        return {"error": "Application not found"}

    log = json.loads(application.agent_log or "[]")
    _log_step(log, "submitted", "completed", "Application submitted successfully")

    application.status = "completed"
    application.applied_at = datetime.datetime.utcnow()
    application.agent_log = json.dumps(log)
    db.commit()

    return {"status": "completed", "application_id": application.id}


async def answer_question(db: Session, question_id: int, answer: str) -> dict:
    """Answer a pending agent question and check if application can proceed."""
    question = db.query(AgentQuestion).filter(AgentQuestion.id == question_id).first()
    if not question:
        return {"error": "Question not found"}

    question.answer = answer
    question.is_answered = True
    question.answered_at = datetime.datetime.utcnow()
    db.commit()

    # Check if all questions for this application are answered
    if question.application_id:
        pending = db.query(AgentQuestion).filter(
            AgentQuestion.application_id == question.application_id,
            AgentQuestion.is_answered == False,
        ).count()

        if pending == 0:
            # Resume application
            application = db.query(Application).filter(
                Application.id == question.application_id
            ).first()
            if application and application.status == "needs_input":
                # Store answers in profile additional_info for future use
                profile = application.profile
                additional_info = _safe_json(profile.additional_info, {})
                additional_info[question.question] = answer
                profile.additional_info = json.dumps(additional_info)

                result = await process_application(db, application)
                return {"status": "resumed", "result": result}

    return {"status": "answered", "pending_questions": True}
