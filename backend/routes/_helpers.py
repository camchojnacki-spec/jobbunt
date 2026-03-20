"""Shared helpers used across multiple route modules."""
import logging
import os

from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.models.models import Job, Profile, Company
from backend.services.enrichment import enrich_job
from backend.services.scorer import score_and_update_job_ai

logger = logging.getLogger(__name__)

# Progress tracking for long-running tasks
_rescore_progress = {}  # {profile_id: {"current": n, "total": n, "status": "running"|"done"|"error"}}

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ── Cloud Storage (production) ────────────────────────────────────────────
GCS_BUCKET = os.environ.get("GCS_BUCKET")
_gcs_client = None


def _get_gcs_bucket():
    """Lazy-load GCS bucket for production file storage."""
    global _gcs_client
    if not GCS_BUCKET:
        return None
    if _gcs_client is None:
        from google.cloud import storage
        _gcs_client = storage.Client()
    return _gcs_client.bucket(GCS_BUCKET)


def _get_profile_for_user(profile_id: int, user, db: Session) -> Profile:
    """Get a profile by ID, checking ownership if user is authenticated."""
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")
    if user and profile.user_id and profile.user_id != user.id:
        raise HTTPException(403, "Not your profile")
    return profile


async def _safe_enrich(db: Session, job: Job, profile: Profile):
    """Wrapper for enrich_job that catches exceptions for use with asyncio.gather."""
    try:
        await enrich_job(db, job, profile)
    except Exception as e:
        logger.warning(f"Enrichment failed for job {job.id}: {e}")


async def _safe_score(db: Session, job: Job, profile: Profile):
    """Wrapper for scoring that falls back to rule-based on error."""
    from backend.services.scorer import score_and_update_job
    company = db.query(Company).filter(Company.id == job.company_id).first() if job.company_id else None
    try:
        await score_and_update_job_ai(db, job, profile, company)
    except Exception as e:
        logger.warning(f"AI scoring failed for job {job.id}, falling back to rule-based: {e}")
        score_and_update_job(db, job, profile, company)
