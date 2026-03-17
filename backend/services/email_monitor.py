"""Email monitoring service - checks Gmail for application status updates.

Scans for:
- Application confirmations (BambooHR, Greenhouse, Lever, Workday, etc.)
- Interview invitations
- Rejections / "not moving forward" messages
- Follow-up requests

Uses AI to classify email intent and match to existing applications.
"""
import json
import logging
import re
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from backend.models.models import Job, Application, Company
from backend.services.ai import ai_generate_json

logger = logging.getLogger(__name__)

# Common ATS senders
ATS_SENDERS = [
    "bamboohr.com", "greenhouse.io", "lever.co", "workday.com",
    "icims.com", "jobvite.com", "smartrecruiters.com", "taleo.net",
    "successfactors.com", "ultipro.com", "myworkdayjobs.com",
    "ashbyhq.com", "breezy.hr", "jazz.co",
]

# Email classification patterns
CONFIRMATION_PATTERNS = [
    r"(?:thank|thanks).*(?:appl|interest|submitting)",
    r"(?:received|confirm).*(?:application|submission)",
    r"application.*(?:received|submitted|confirmed)",
    r"we.*received.*your.*application",
]

INTERVIEW_PATTERNS = [
    r"interview.*(?:schedule|invitation|invite|request)",
    r"(?:schedule|book).*(?:call|meeting|interview|chat)",
    r"(?:next\s+step|move\s+forward).*(?:interview|conversation)",
    r"(?:like|love)\s+to\s+(?:speak|talk|meet|chat|connect)",
    r"(?:screening|phone\s+screen|video\s+call)",
]

REJECTION_PATTERNS = [
    r"(?:unfortunately|regret).*(?:not|won't|unable)",
    r"(?:decided|chosen).*(?:not|other|different)\s+(?:to|candidate)",
    r"(?:not\s+moving|won't\s+be\s+moving)\s+forward",
    r"position\s+(?:has\s+been\s+)?filled",
    r"(?:pursue|consider)\s+other\s+candidates",
]


def classify_email_basic(subject: str, snippet: str) -> Optional[str]:
    """Quick regex-based classification of email intent."""
    text = f"{subject} {snippet}".lower()

    for pattern in INTERVIEW_PATTERNS:
        if re.search(pattern, text):
            return "interview"

    for pattern in REJECTION_PATTERNS:
        if re.search(pattern, text):
            return "rejected"

    for pattern in CONFIRMATION_PATTERNS:
        if re.search(pattern, text):
            return "confirmed"

    return None


async def classify_email_ai(subject: str, snippet: str, from_addr: str) -> Optional[dict]:
    """Use AI to classify an email related to job applications."""
    prompt = f"""Classify this email related to a job application.

Subject: {subject}
From: {from_addr}
Preview: {snippet[:500]}

Return JSON:
{{
    "classification": "confirmed|interview|rejected|follow_up|irrelevant",
    "company_name": "company name mentioned or inferred from sender",
    "job_title": "job title if mentioned, or null",
    "confidence": 0.0-1.0,
    "summary": "1-sentence summary of what the email says"
}}

Rules:
- "confirmed" = they received the application
- "interview" = they want to schedule an interview or next steps
- "rejected" = they're not moving forward
- "follow_up" = they need more info or documents from the candidate
- "irrelevant" = not related to a job application"""

    return await ai_generate_json(prompt, max_tokens=300, model_tier="flash")


def match_email_to_application(
    db: Session,
    profile_id: int,
    company_name: str,
    job_title: Optional[str] = None,
) -> Optional[tuple[Job, Application]]:
    """Try to match an email to an existing application based on company/job title."""
    # First try exact company match
    companies = db.query(Company).filter(
        Company.name.ilike(f"%{company_name}%")
    ).all()

    if not companies:
        return None

    company_ids = [c.id for c in companies]

    # Find jobs at these companies that have applications
    query = (
        db.query(Job, Application)
        .join(Application, Application.job_id == Job.id)
        .filter(
            Job.profile_id == profile_id,
            Job.company_id.in_(company_ids),
        )
    )

    # If we have a job title, prioritize that match
    if job_title:
        title_match = query.filter(Job.title.ilike(f"%{job_title}%")).first()
        if title_match:
            return title_match

    # Otherwise return the most recent application at this company
    result = query.order_by(Application.created_at.desc()).first()
    return result


def update_application_status(
    db: Session,
    application: Application,
    job: Job,
    new_status: str,
    email_summary: str,
) -> dict:
    """Update application and job status based on email classification."""
    old_status = application.status
    changes = {"old_status": old_status, "new_status": new_status}

    if new_status == "confirmed":
        if application.status not in ("submitted",):
            application.status = "submitted"
            job.status = "applied"
            if not job.applied_at:
                job.applied_at = datetime.utcnow()
            changes["action"] = "Marked as submitted"

    elif new_status == "interview":
        application.status = "interview"
        job.status = "interview"
        changes["action"] = "Interview stage! 🎉"

    elif new_status == "rejected":
        application.status = "rejected"
        job.status = "rejected"
        changes["action"] = "Application declined"

    elif new_status == "follow_up":
        changes["action"] = "Follow-up needed"
        # Don't change status, just flag it

    # Add to application timeline
    try:
        log = json.loads(application.agent_log or "[]")
    except (json.JSONDecodeError, TypeError):
        log = []
    log.append({
        "step": f"Email: {new_status}",
        "status": "completed",
        "details": email_summary,
        "timestamp": datetime.utcnow().isoformat(),
    })
    application.agent_log = json.dumps(log)

    db.commit()
    return changes
