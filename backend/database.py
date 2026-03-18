"""Database setup and session management."""
import os
import logging
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker, DeclarativeBase

logger = logging.getLogger(__name__)

# ── Database URL ──────────────────────────────────────────────────────────
# Set DATABASE_URL env var for production (PostgreSQL on Cloud SQL).
# Falls back to local SQLite for development (zero-config).

_DATABASE_URL = os.environ.get("DATABASE_URL")

if _DATABASE_URL:
    # Production: PostgreSQL (Cloud SQL, Neon, Supabase, etc.)
    logger.info("Using PostgreSQL database")
    engine = create_engine(
        _DATABASE_URL,
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,  # Recycle connections every 30 min (Cloud SQL best practice)
        pool_pre_ping=True,  # Verify connections are alive before using
    )
else:
    # Local dev: SQLite
    DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "jobbunt.db")
    _DATABASE_URL = f"sqlite:///{DB_PATH}"
    logger.info(f"Using SQLite database at {DB_PATH}")
    engine = create_engine(_DATABASE_URL, connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _is_sqlite() -> bool:
    """Check if the current engine is SQLite."""
    return "sqlite" in str(engine.url)


def _run_migrations():
    """Add any new columns to existing tables."""
    inspector = inspect(engine)
    migrations = [
        ("profiles", "advisor_data", "TEXT"),
        ("profiles", "user_id", "INTEGER REFERENCES users(id)" if _is_sqlite() else "INTEGER REFERENCES users(id)"),
        ("profiles", "career_history", "TEXT"),
        ("profile_questions", "purpose", "TEXT"),
        # Reporter Corner fields
        ("profiles", "availability", "VARCHAR(100)"),
        ("profiles", "employment_type", "VARCHAR(100)"),
        ("profiles", "commute_tolerance", "VARCHAR(100)"),
        ("profiles", "relocation", "VARCHAR(100)"),
        ("profiles", "company_size", "VARCHAR(200)"),
        ("profiles", "industry_preference", "TEXT"),
        ("profiles", "top_priority", "TEXT"),
        ("profiles", "security_clearance", "VARCHAR(100)"),
        ("profiles", "travel_willingness", "VARCHAR(100)"),
        ("profiles", "additional_notes", "TEXT"),
    ]
    with engine.connect() as conn:
        for table, column, col_type in migrations:
            try:
                existing = [c["name"] for c in inspector.get_columns(table)]
            except Exception:
                continue  # Table doesn't exist yet
            if column not in existing:
                try:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
                    conn.commit()
                    logger.info(f"Migration: added {table}.{column}")
                except Exception as e:
                    logger.warning(f"Migration failed for {table}.{column}: {e}")


def init_db():
    from backend.models.models import Job, Application, Profile, AgentQuestion, Company, User  # noqa
    Base.metadata.create_all(bind=engine)
    _run_migrations()
