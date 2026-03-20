"""Application pipeline API routes."""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from backend.database import get_db
from backend.models.models import Application, AgentQuestion, Company, User
from backend.services.agent import answer_question, submit_application
from backend.serializers import _application_dict, _question_dict

logger = logging.getLogger(__name__)
router = APIRouter()


class AnswerSubmit(BaseModel):
    answer: str


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


@router.put("/applications/{app_id}")
async def update_application(app_id: int, data: dict = Body(...), db: Session = Depends(get_db)):
    """Update an application's pipeline status and/or notes."""
    application = db.query(Application).filter(Application.id == app_id).first()
    if not application:
        raise HTTPException(404, "Application not found")
    valid_pipeline_statuses = {"applied", "screening", "interview", "offer", "accepted", "rejected", "no_response"}
    if "pipeline_status" in data and data["pipeline_status"] in valid_pipeline_statuses:
        application.pipeline_status = data["pipeline_status"]
    if "notes" in data:
        application.notes = data["notes"]
    db.commit()
    return _application_dict(application)


@router.post("/applications/{app_id}/submit")
async def mark_submitted(app_id: int, db: Session = Depends(get_db)):
    """Mark an application as submitted (after browser-based submission)."""
    result = await submit_application(db, app_id)
    return result


@router.post("/questions/{question_id}/answer")
async def submit_answer(question_id: int, data: AnswerSubmit, db: Session = Depends(get_db)):
    result = await answer_question(db, question_id, data.answer)
    return result
