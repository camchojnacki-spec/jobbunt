"""Main FastAPI application."""
import os
import time
import logging
from collections import defaultdict

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from backend.database import init_db, get_db, SessionLocal
from backend.routes.api import router as api_router
from backend.routes.auth import router as auth_router
from backend.auth import auth_enabled, get_user_from_request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Rate Limiting (protects AI/GCP billing) ──────────────────────────────
# Simple in-memory rate limiter — tracks requests per IP per window
_rate_limits: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_AI = int(os.getenv("RATE_LIMIT_AI", "30"))  # AI calls per minute
RATE_LIMIT_MAX_SEARCH = int(os.getenv("RATE_LIMIT_SEARCH", "10"))  # searches per minute
RATE_LIMIT_MAX_AUTH = int(os.getenv("RATE_LIMIT_AUTH", "20"))  # auth attempts per minute

# Paths that consume AI/GCP resources
AI_PATHS = {"/api/jobs/search", "/api/profiles/", "/api/config/prompts/"}
AI_KEYWORDS = {"search", "score", "generate", "enrich", "analyze", "enhance"}
SEARCH_PATHS = {"/api/jobs/search"}

def _check_rate_limit(key: str, max_requests: int) -> bool:
    """Return True if under limit, False if exceeded."""
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW
    # Clean old entries
    _rate_limits[key] = [t for t in _rate_limits[key] if t > window_start]
    if len(_rate_limits[key]) >= max_requests:
        return False
    _rate_limits[key].append(now)
    return True

app = FastAPI(title="Jobbunt", description="Tinder-style job search & auto-apply")

# CORS — allow browser-scraped pages to POST jobs back to localhost
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize database
init_db()

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
LOGIN_PAGE = os.path.join(STATIC_DIR, "login.html")


@app.middleware("http")
async def auth_and_rate_limit_middleware(request: Request, call_next):
    """Enforce authentication + rate limiting.

    - /auth/* routes are always allowed (login flow)
    - /static/* files are always allowed
    - /health is always allowed
    - AI-heavy endpoints are rate-limited to prevent GCP bill blowups
    - /api/* returns 401 JSON if not authenticated (when auth enabled)
    """
    path = request.url.path
    client_ip = request.client.host if request.client else "unknown"

    # ── Rate limiting (always active, even in dev mode) ──
    # Auth endpoints (prevent brute force)
    if path.startswith("/auth/local/"):
        if not _check_rate_limit(f"auth:{client_ip}", RATE_LIMIT_MAX_AUTH):
            return JSONResponse(status_code=429, content={"detail": "Too many auth attempts. Try again in a minute."})

    # Search endpoints (expensive: scraping + AI scoring)
    if path in SEARCH_PATHS and request.method == "POST":
        if not _check_rate_limit(f"search:{client_ip}", RATE_LIMIT_MAX_SEARCH):
            return JSONResponse(status_code=429, content={"detail": "Search rate limit reached. Try again in a minute."})

    # AI-heavy endpoints
    if path.startswith("/api/") and request.method in ("POST", "PUT"):
        path_lower = path.lower()
        if any(kw in path_lower for kw in AI_KEYWORDS):
            if not _check_rate_limit(f"ai:{client_ip}", RATE_LIMIT_MAX_AI):
                return JSONResponse(status_code=429, content={"detail": "AI rate limit reached. Try again in a minute."})

    # ── Auth enforcement ──
    # Skip auth entirely if not configured (dev mode)
    if not auth_enabled():
        return await call_next(request)

    # Always allow auth routes, static files, health check, and login page
    if (path.startswith("/auth/")
            or path.startswith("/static/")
            or path == "/health"
            or path == "/login"):
        return await call_next(request)

    # Check session
    db = SessionLocal()
    try:
        user = get_user_from_request(request, db)
    finally:
        db.close()

    if not user:
        # API routes get a 401 JSON response
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
        # Main page and other routes redirect to login
        return RedirectResponse("/login", status_code=302)

    return await call_next(request)


# Auth routes (must be before API routes)
app.include_router(auth_router)

# API routes
app.include_router(api_router)

# Serve static files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/login")
async def login_page():
    """Serve the login page."""
    if os.path.exists(LOGIN_PAGE):
        return FileResponse(LOGIN_PAGE)
    # Fallback: redirect to OAuth login directly
    return RedirectResponse("/auth/login")


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/health")
async def health():
    return {"status": "ok"}
