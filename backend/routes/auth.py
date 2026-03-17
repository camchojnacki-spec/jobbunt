"""Authentication routes for Jobbunt."""
import datetime
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from backend.database import get_db
from backend.models.models import User, Profile
from backend.auth import (
    auth_enabled,
    verify_google_token,
    create_jwt,
    get_current_user,
    GOOGLE_CLIENT_ID,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth")


class GoogleAuthRequest(BaseModel):
    token: str  # Google ID token from frontend


@router.get("/config")
def auth_config():
    """Return auth configuration for the frontend."""
    return {
        "auth_enabled": auth_enabled(),
        "google_client_id": GOOGLE_CLIENT_ID if auth_enabled() else None,
    }


@router.post("/google")
def google_login(data: GoogleAuthRequest, db: Session = Depends(get_db)):
    """Verify Google token, find or create user, return JWT + user info."""
    if not auth_enabled():
        raise HTTPException(503, "Google OAuth is not configured")

    idinfo = verify_google_token(data.token)

    google_id = idinfo["sub"]
    email = idinfo.get("email", "")
    name = idinfo.get("name", "")
    picture = idinfo.get("picture", "")

    # Find or create user
    user = db.query(User).filter(User.google_id == google_id).first()
    if user:
        # Update last login and any changed info
        user.last_login = datetime.datetime.utcnow()
        user.email = email
        user.name = name
        user.picture_url = picture
        db.commit()
    else:
        user = User(
            google_id=google_id,
            email=email,
            name=name,
            picture_url=picture,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info(f"New user created: {email} (id={user.id})")

    token = create_jwt(user.id, email)

    return {
        "token": token,
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "picture_url": user.picture_url,
        },
    }


@router.get("/me")
def get_me(user: User = Depends(get_current_user)):
    """Return current user info."""
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "picture_url": user.picture_url,
    }


@router.post("/claim-profiles")
def claim_profiles(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Claim all unclaimed profiles (user_id IS NULL) for the current user."""
    unclaimed = db.query(Profile).filter(Profile.user_id.is_(None)).all()
    claimed_count = 0
    for profile in unclaimed:
        profile.user_id = user.id
        claimed_count += 1
    db.commit()
    logger.info(f"User {user.email} claimed {claimed_count} profiles")
    return {"claimed": claimed_count}
