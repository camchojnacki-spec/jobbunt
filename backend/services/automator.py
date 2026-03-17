"""Browser automation service for job application submission.

Orchestrates the full apply flow:
1. Navigate to job posting URL
2. Detect application platform and form type
3. Fill form fields with candidate data
4. Upload resume
5. Paste cover letter
6. Handle account creation / sign-in if needed
7. Submit or pause for user approval

This module produces step-by-step instructions and data payloads
that can be executed by the Chrome MCP tools or presented to the user
for manual completion.
"""
import json
import logging
from typing import Optional

from sqlalchemy.orm import Session

from backend.models.models import Job, Application, Profile, Company
from backend.services.ai import ai_generate_json, get_provider

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


# ── Platform-specific strategies ─────────────────────────────────────────

PLATFORM_STRATEGIES = {
    "linkedin": {
        "name": "LinkedIn",
        "supports_easy_apply": True,
        "steps": [
            "Navigate to the job posting URL",
            "Click 'Easy Apply' or 'Apply' button",
            "Fill in contact info (name, email, phone)",
            "Upload resume (PDF preferred)",
            "Add cover letter if field is available",
            "Answer any screening questions",
            "Review and submit",
        ],
        "selectors": {
            "apply_btn": ".jobs-apply-button, .jobs-s-apply button",
            "easy_apply_btn": ".jobs-apply-button--top-card",
            "name_field": "input[name='firstName'], input[name='lastName']",
            "email_field": "input[name='email'], input[type='email']",
            "phone_field": "input[name='phone'], input[type='tel']",
            "resume_upload": "input[type='file']",
            "submit_btn": "button[aria-label='Submit application'], button[aria-label='Review']",
            "next_btn": "button[aria-label='Continue to next step'], footer button.artdeco-button--primary",
        },
    },
    "indeed": {
        "name": "Indeed",
        "supports_easy_apply": True,
        "steps": [
            "Navigate to the job posting URL",
            "Click 'Apply now' button",
            "Sign in or create Indeed account if needed",
            "Upload/select resume",
            "Fill in any additional fields",
            "Answer screening questions",
            "Review and submit",
        ],
        "selectors": {
            "apply_btn": "#indeedApplyButton, .jobsearch-IndeedApplyButton-newDesign",
            "resume_upload": "input[type='file']",
            "submit_btn": "button[type='submit']",
            "next_btn": ".ia-continueButton",
        },
    },
    "workday": {
        "name": "Workday",
        "steps": [
            "Navigate to the job posting URL",
            "Click 'Apply' button",
            "Create Workday account or sign in",
            "Fill in personal information",
            "Upload resume",
            "Fill in work experience (or auto-populate from resume)",
            "Fill in education",
            "Answer application questions",
            "Add cover letter",
            "Review and submit",
        ],
        "selectors": {
            "apply_btn": "a[data-automation-id='jobPostingApplyButton']",
            "sign_in_btn": "a[data-automation-id='signInLink']",
            "create_account_btn": "a[data-automation-id='createAccountLink']",
            "email_field": "input[data-automation-id='email']",
            "resume_upload": "input[type='file']",
            "submit_btn": "button[data-automation-id='bottom-navigation-next-button']",
        },
    },
    "greenhouse": {
        "name": "Greenhouse",
        "steps": [
            "Navigate to the job posting URL",
            "Click 'Apply for this job' button",
            "Fill in name, email, phone",
            "Upload resume",
            "Upload or paste cover letter",
            "Fill in LinkedIn URL if available",
            "Answer custom questions",
            "Submit application",
        ],
        "selectors": {
            "apply_btn": "#submit_app, .btn-apply",
            "first_name": "#first_name",
            "last_name": "#last_name",
            "email_field": "#email",
            "phone_field": "#phone",
            "resume_upload": "#resume input[type='file'], input[name='resume']",
            "cover_letter_upload": "#cover_letter input[type='file']",
            "cover_letter_text": "#cover_letter_text, textarea[name='cover_letter_text']",
            "submit_btn": "#submit_app",
        },
    },
    "lever": {
        "name": "Lever",
        "steps": [
            "Navigate to the job posting URL",
            "Click 'Apply for this job' button",
            "Fill in name, email, phone, current company, LinkedIn",
            "Upload resume",
            "Add cover letter",
            "Answer additional questions",
            "Submit application",
        ],
        "selectors": {
            "apply_btn": ".postings-btn-wrapper a",
            "name_field": "input[name='name']",
            "email_field": "input[name='email']",
            "phone_field": "input[name='phone']",
            "resume_upload": "input[name='resume']",
            "cover_letter_text": "textarea[name='comments']",
            "submit_btn": "button[type='submit']",
        },
    },
    "company_direct": {
        "name": "Company Website",
        "steps": [
            "Navigate to the job posting URL",
            "Look for 'Apply' or 'Submit Application' button",
            "Create account if required",
            "Fill in application form fields",
            "Upload resume",
            "Add cover letter",
            "Answer any screening questions",
            "Review and submit",
        ],
        "selectors": {},
    },
    "other": {
        "name": "External Site",
        "steps": [
            "Navigate to the job posting URL",
            "Identify the application method",
            "Follow the application process",
            "Fill in required fields",
            "Upload resume and cover letter",
            "Submit application",
        ],
        "selectors": {},
    },
}


def get_platform_strategy(platform: str) -> dict:
    """Get the automation strategy for a given platform."""
    return PLATFORM_STRATEGIES.get(platform, PLATFORM_STRATEGIES["other"])


async def build_automation_plan(
    db: Session, application: Application
) -> dict:
    """Build a detailed automation plan for submitting an application.

    Returns a structured plan with:
    - steps: ordered list of actions to perform
    - form_data: pre-filled data for form fields
    - files: files to upload
    - platform_info: platform-specific guidance
    """
    job = application.job
    profile = application.profile
    company = None
    if job.company_id:
        company = db.query(Company).filter(Company.id == job.company_id).first()

    log = json.loads(application.agent_log or "[]")

    # Detect platform
    platform = "other"
    for entry in log:
        if entry.get("platform"):
            platform = entry["platform"]
            break

    if platform == "other" and job.url:
        from backend.services.agent import _detect_platform
        platform = _detect_platform(job.url)

    strategy = get_platform_strategy(platform)

    # Build form data
    skills = _safe_json(profile.skills, [])
    additional = _safe_json(profile.additional_info, {})

    form_data = {
        "first_name": profile.name.split()[0] if profile.name else "",
        "last_name": " ".join(profile.name.split()[1:]) if profile.name and " " in profile.name else "",
        "full_name": profile.name or "",
        "email": profile.email or "",
        "phone": profile.phone or "",
        "location": profile.location or "",
        "current_title": (_safe_json(profile.target_roles, []) or [""])[0],
        "experience_years": profile.experience_years,
        "skills_text": ", ".join(skills),
        "linkedin_url": additional.get("linkedin_url", ""),
        "website_url": additional.get("website_url", ""),
        "salary_expectation": profile.min_salary,
    }

    # Files
    files = {}
    if profile.resume_path:
        files["resume"] = {
            "path": profile.resume_path,
            "type": "resume",
            "format": profile.resume_path.split(".")[-1] if profile.resume_path else "pdf",
        }

    # Use AI to generate smart automation instructions if available
    ai_instructions = None
    if get_provider() != "none" and job.description:
        ai_instructions = await _ai_automation_plan(job, profile, platform, strategy)

    plan = {
        "application_id": application.id,
        "job_id": job.id,
        "platform": platform,
        "platform_name": strategy["name"],
        "url": job.url,
        "steps": ai_instructions.get("steps", strategy["steps"]) if ai_instructions else strategy["steps"],
        "selectors": strategy.get("selectors", {}),
        "form_data": form_data,
        "cover_letter": application.cover_letter,
        "files": files,
        "requires_account": ai_instructions.get("requires_account", platform in ["workday", "indeed"]) if ai_instructions else platform in ["workday", "indeed"],
        "requires_signin": ai_instructions.get("requires_signin", False) if ai_instructions else False,
        "screening_answers": additional,
        "notes": ai_instructions.get("notes", "") if ai_instructions else "",
    }

    return plan


async def _ai_automation_plan(job: Job, profile: Profile, platform: str, strategy: dict) -> dict:
    """Use AI to generate smart, context-aware automation instructions."""
    prompt = f"""You are a job application automation expert. Analyze this job posting and create specific step-by-step instructions for submitting an application.

Platform: {strategy['name']} ({platform})
Job URL: {job.url}
Job Title: {job.title}
Company: {job.company}
Description excerpt: {(job.description or '')[:2000]}

The candidate has:
- Full name, email, phone, location
- Resume file ready to upload
- Cover letter already generated
- {profile.experience_years or 'Unknown'} years of experience

Return JSON:
{{
    "steps": ["detailed step 1", "detailed step 2", ...],
    "requires_account": true/false,
    "requires_signin": true/false,
    "expected_questions": ["screening questions that might be asked"],
    "notes": "any important observations about this specific application"
}}

Be specific about what to click, what to fill in, and what to watch out for.
Keep steps actionable and concise."""

    return await ai_generate_json(prompt, max_tokens=800, model_tier="fast") or {}


async def execute_step(step_index: int, plan: dict, action: str = "auto") -> dict:
    """Execute a single step in the automation plan.

    This is designed to be called iteratively, with the frontend/Chrome MCP
    handling the actual browser interaction.

    Returns:
    - status: 'completed', 'needs_user', 'failed'
    - next_step: index of next step to execute
    - instruction: what to do next
    - selector: CSS selector to interact with (if applicable)
    """
    steps = plan.get("steps", [])
    if step_index >= len(steps):
        return {
            "status": "completed",
            "message": "All steps completed",
        }

    current_step = steps[step_index]

    return {
        "status": "pending",
        "step_index": step_index,
        "total_steps": len(steps),
        "instruction": current_step,
        "form_data": plan.get("form_data", {}),
        "selectors": plan.get("selectors", {}),
        "next_step": step_index + 1,
    }
