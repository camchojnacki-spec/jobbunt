"""Main FastAPI application."""
import os
import logging

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
async def auth_middleware(request: Request, call_next):
    """Enforce authentication when OAuth is enabled.

    - /auth/* routes are always allowed (login flow)
    - /static/* files are always allowed
    - /health is always allowed
    - /api/* returns 401 JSON if not authenticated
    - / redirects to login page if not authenticated
    """
    # Skip auth entirely if not configured (dev mode)
    if not auth_enabled():
        return await call_next(request)

    path = request.url.path

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
            # Allow /api/auth/config without auth (frontend needs it to know if auth is enabled)
            # Note: auth routes are now at /auth/ not /api/auth/, but keep this for safety
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
