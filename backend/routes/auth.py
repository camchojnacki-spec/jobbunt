"""Authentication routes for Jobbunt — server-side Google OAuth flow."""
import datetime
import logging
import urllib.parse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse
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
    }


@router.get("/login")
async def login(request: Request):
    """Redirect to Google's OAuth consent screen."""
    if not auth_enabled():
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
async def logout():
    """Clear session cookie and redirect to /."""
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return response


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
