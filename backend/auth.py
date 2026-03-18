"""Google OAuth + session cookie authentication for Jobbunt."""
import os
import time
import logging
from typing import Optional

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from fastapi import Request, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.models import User

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", os.getenv("JWT_SECRET", "dev-fallback-secret-key"))
SESSION_COOKIE_NAME = "jobbunt_session"
SESSION_MAX_AGE = 7 * 24 * 3600  # 7 days in seconds
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "")  # e.g. http://localhost:8000/auth/callback

_serializer = URLSafeTimedSerializer(SESSION_SECRET)


def auth_enabled() -> bool:
    """Return True if authentication is active (Google OAuth or local auth).
    When DEV_SKIP_AUTH is set, auth is bypassed entirely (single-user dev mode).
    """
    if os.getenv("DEV_SKIP_AUTH", "").lower() in ("1", "true", "yes"):
        return False
    # Auth is enabled if Google OAuth is configured OR local auth is being used
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET) or bool(os.getenv("LOCAL_AUTH", ""))


def get_redirect_uri(request: Request) -> str:
    """Build the OAuth redirect URI from the request or env."""
    if OAUTH_REDIRECT_URI:
        return OAUTH_REDIRECT_URI
    # Auto-detect from the incoming request
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    return f"{scheme}://{host}/auth/callback"


def create_session_cookie(user_id: int) -> str:
    """Create a signed session cookie value containing the user_id."""
    return _serializer.dumps({"user_id": user_id})


def decode_session_cookie(cookie_value: str) -> Optional[dict]:
    """Decode and verify a session cookie. Returns payload or None."""
    try:
        return _serializer.loads(cookie_value, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def get_user_from_request(request: Request, db: Session) -> Optional[User]:
    """Extract user from session cookie if present and valid."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return None
    payload = decode_session_cookie(cookie)
    if not payload or "user_id" not in payload:
        return None
    user = db.query(User).filter(User.id == payload["user_id"]).first()
    return user


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """FastAPI dependency: requires a valid session and returns the User. 401 if missing/invalid."""
    if not auth_enabled():
        raise HTTPException(503, "Auth not configured")
    user = get_user_from_request(request, db)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return user


def get_optional_user(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[User]:
    """FastAPI dependency: returns User if valid session present, None otherwise.
    Used for gradual migration - endpoints still work without auth."""
    if not auth_enabled():
        return None
    return get_user_from_request(request, db)
