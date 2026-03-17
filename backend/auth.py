"""Google OAuth + JWT authentication for Jobbunt."""
import os
import logging
import datetime
from typing import Optional

import jwt
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from fastapi import Header, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.models import User

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24 * 30  # 30 days


def auth_enabled() -> bool:
    """Return True if Google OAuth is configured."""
    return bool(GOOGLE_CLIENT_ID and JWT_SECRET)


def verify_google_token(token: str) -> dict:
    """Verify a Google ID token and return the payload (sub, email, name, picture)."""
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(503, "Google OAuth is not configured on this server")
    try:
        idinfo = id_token.verify_oauth2_token(
            token, google_requests.Request(), GOOGLE_CLIENT_ID
        )
        if idinfo["iss"] not in ("accounts.google.com", "https://accounts.google.com"):
            raise ValueError("Invalid issuer")
        return idinfo
    except ValueError as e:
        logger.warning(f"Google token verification failed: {e}")
        raise HTTPException(401, f"Invalid Google token: {e}")


def create_jwt(user_id: int, email: str) -> str:
    """Create a JWT for the given user."""
    payload = {
        "user_id": user_id,
        "email": email,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=JWT_EXPIRY_HOURS),
        "iat": datetime.datetime.utcnow(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict:
    """Decode and verify a JWT. Raises HTTPException on failure."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(401, f"Invalid token: {e}")


def get_current_user(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> User:
    """FastAPI dependency: requires a valid JWT and returns the User. 401 if missing/invalid."""
    if not auth_enabled():
        raise HTTPException(503, "Auth not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing authorization header")
    token = authorization.split(" ", 1)[1]
    payload = decode_jwt(token)
    user = db.query(User).filter(User.id == payload["user_id"]).first()
    if not user:
        raise HTTPException(401, "User not found")
    return user


def get_optional_user(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """FastAPI dependency: returns User if valid JWT present, None otherwise.
    Used for gradual migration - endpoints still work without auth."""
    if not auth_enabled():
        return None
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        token = authorization.split(" ", 1)[1]
        payload = decode_jwt(token)
        user = db.query(User).filter(User.id == payload["user_id"]).first()
        return user
    except Exception:
        return None
