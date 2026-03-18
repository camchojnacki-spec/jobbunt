"""Authentication routes for Jobbunt — server-side Google OAuth + local auth."""
import datetime
import hashlib
import logging
import re
import secrets
import urllib.parse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.models import User, Profile
from backend.auth import (
    auth_enabled,
    get_current_user,
    get_redirect_uri,
    create_session_cookie,
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
)


# ── Simple password hashing (no bcrypt dependency needed) ────────────────
def _hash_password(password: str) -> str:
    """Hash password using PBKDF2-SHA256 (stdlib, no extra deps)."""
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${h.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    """Verify a password against a stored hash."""
    try:
        salt, hash_hex = stored.split("$", 1)
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
        return h.hex() == hash_hex
    except Exception:
        return False

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


@router.get("/config")
def auth_config():
    """Return auth configuration for the frontend."""
    return {
        "auth_enabled": auth_enabled(),
        "google_client_id": GOOGLE_CLIENT_ID if auth_enabled() else None,
        "local_auth_enabled": True,  # Always allow local login/register
    }


@router.get("/login")
async def login(request: Request):
    """Redirect to Google's OAuth consent screen."""
    if not auth_enabled() and not request.query_params.get("force"):
        return RedirectResponse("/")

    redirect_uri = get_redirect_uri(request)
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account",
    }
    url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url)


@router.get("/dev-login")
async def dev_login(request: Request):
    """Dev-only: trigger Google OAuth even when DEV_SKIP_AUTH is set, to capture profile photo."""
    if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        redirect_uri = get_redirect_uri(request)
        params = {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "offline",
            "prompt": "select_account",
        }
        url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
        return RedirectResponse(url)
    return JSONResponse({"error": "Google OAuth not configured"}, status_code=400)


@router.get("/callback")
async def callback(request: Request, code: str = "", error: str = "", db: Session = Depends(get_db)):
    """Handle the OAuth callback from Google."""
    if error:
        logger.warning(f"OAuth error: {error}")
        return RedirectResponse("/auth/login")

    if not code:
        raise HTTPException(400, "Missing authorization code")

    redirect_uri = get_redirect_uri(request)

    # Exchange authorization code for tokens
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if token_resp.status_code != 200:
        logger.error(f"Token exchange failed: {token_resp.text}")
        raise HTTPException(502, "Failed to exchange authorization code")

    tokens = token_resp.json()
    access_token = tokens.get("access_token")
    if not access_token:
        raise HTTPException(502, "No access token in response")

    # Fetch user info
    async with httpx.AsyncClient() as client:
        userinfo_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if userinfo_resp.status_code != 200:
        logger.error(f"Userinfo fetch failed: {userinfo_resp.text}")
        raise HTTPException(502, "Failed to fetch user info")

    userinfo = userinfo_resp.json()
    google_id = userinfo.get("sub", "")
    email = userinfo.get("email", "")
    name = userinfo.get("name", "")
    picture = userinfo.get("picture", "")

    if not google_id:
        raise HTTPException(502, "No user ID from Google")

    # Find or create user
    user = db.query(User).filter(User.google_id == google_id).first()
    if user:
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

    # Create session cookie and redirect to app
    session_value = create_session_cookie(user.id)
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_value,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@router.get("/logout")
async def logout(request: Request):
    """Clear session cookie. Returns JSON for AJAX calls, redirects for browser navigation."""
    accept = request.headers.get("accept", "")
    if "application/json" in accept or "fetch" in request.headers.get("sec-fetch-mode", ""):
        response = JSONResponse({"status": "logged_out"})
    else:
        response = RedirectResponse("/", status_code=302)
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return response


# ── Local Auth (email + password, no Google required) ────────────────────

class LocalRegister(BaseModel):
    name: str
    email: str
    password: str

class LocalLogin(BaseModel):
    email: str
    password: str


@router.post("/local/register")
async def local_register(data: LocalRegister, request: Request, db: Session = Depends(get_db)):
    """Register a new local user (no Google OAuth needed)."""
    if not data.name.strip() or not data.email.strip() or not data.password.strip():
        raise HTTPException(400, "Name, email, and password are required")

    # Email format validation
    email_pattern = r'^[^@\s]+@[^@\s]+\.[^@\s]+$'
    if not re.match(email_pattern, data.email.strip()):
        raise HTTPException(400, "Please enter a valid email address")

    # Password complexity requirements
    password = data.password
    if len(password) < 8 or not re.search(r'[A-Z]', password) or not re.search(r'[0-9]', password):
        raise HTTPException(400, "Password must be at least 8 characters, with at least one uppercase letter and one digit")

    # Check if email already exists
    existing = db.query(User).filter(User.email == data.email.strip().lower()).first()
    if existing:
        raise HTTPException(409, "An account with this email already exists. Try logging in.")

    try:
        # Generate a unique local ID (SQLite requires google_id NOT NULL in existing schema)
        local_id = f"local_{secrets.token_hex(16)}"
        user = User(
            google_id=local_id,
            email=data.email.strip().lower(),
            name=data.name.strip(),
            picture_url=None,
            password_hash=_hash_password(data.password),
            auth_provider="local",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info(f"Local user registered: {user.email} (id={user.id})")

        # Create a Profile record pre-populated with name and email
        profile = Profile(
            user_id=user.id,
            name=data.name.strip(),
            email=data.email.strip().lower(),
        )
        db.add(profile)
        db.commit()
        logger.info(f"Profile auto-created for user {user.id}")
    except Exception as e:
        db.rollback()
        logger.error(f"Local registration failed: {e}")
        raise HTTPException(500, f"Registration failed: {e}")

    # Issue session cookie
    session_value = create_session_cookie(user.id)
    response = JSONResponse({"id": user.id, "email": user.email, "name": user.name, "picture_url": None})
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_value,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@router.post("/local/login")
async def local_login(data: LocalLogin, request: Request, db: Session = Depends(get_db)):
    """Log in with email + password."""
    if not data.email.strip() or not data.password.strip():
        raise HTTPException(400, "Email and password are required")

    # Email format validation
    email_pattern = r'^[^@\s]+@[^@\s]+\.[^@\s]+$'
    if not re.match(email_pattern, data.email.strip()):
        raise HTTPException(400, "Please enter a valid email address")

    user = db.query(User).filter(User.email == data.email.strip().lower()).first()
    if not user or not user.password_hash:
        raise HTTPException(401, "Invalid email or password")

    if not _verify_password(data.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")

    user.last_login = datetime.datetime.utcnow()
    db.commit()
    logger.info(f"Local user logged in: {user.email} (id={user.id})")

    session_value = create_session_cookie(user.id)
    response = JSONResponse({
        "id": user.id, "email": user.email, "name": user.name, "picture_url": user.picture_url,
    })
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_value,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@router.get("/me")
def get_me(request: Request, db: Session = Depends(get_db)):
    """Return current user info. Checks session cookie first (local auth), then dev fallback."""
    from backend.auth import get_user_from_request

    # Always check for a valid session cookie first (supports local auth in dev mode too)
    user = get_user_from_request(request, db)
    if user:
        return {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "picture_url": user.picture_url,
        }

    if not auth_enabled():
        # Dev mode with no session — return first user if exists, or a dev stub
        user = db.query(User).first()
        if user:
            return {
                "id": user.id,
                "email": user.email,
                "name": user.name,
                "picture_url": user.picture_url,
            }
        # No user yet — return a stub so the app doesn't break
        return {
            "id": 0,
            "email": "dev@localhost",
            "name": "Dev User",
            "picture_url": None,
        }

    # Production — no valid session means not authenticated
    raise HTTPException(401, "Not authenticated")


@router.post("/claim-profiles")
def claim_profiles(request: Request, db: Session = Depends(get_db)):
    """Claim all unclaimed profiles (user_id IS NULL) for the current user."""
    if not auth_enabled():
        # Dev mode — use first user if exists
        user = db.query(User).first()
        if not user:
            return {"claimed": 0}
    else:
        user = get_current_user(request, db)
    unclaimed = db.query(Profile).filter(Profile.user_id.is_(None)).all()
    claimed_count = 0
    for profile in unclaimed:
        profile.user_id = user.id
        claimed_count += 1
    db.commit()
    logger.info(f"User {user.email} claimed {claimed_count} profiles")
    return {"claimed": claimed_count}
