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
        # Pipeline status tracking for applications
        ("applications", "pipeline_status", "VARCHAR(50) DEFAULT 'applied'"),
        ("applications", "notes", "TEXT"),
        # Per-job user notes
        ("jobs", "user_notes", "TEXT"),
        # New columns on existing tables
        ("jobs", "archived_at", "TIMESTAMP"),
        ("profiles", "archived_at", "TIMESTAMP"),
        ("companies", "updated_at", "TIMESTAMP"),
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


def _create_new_tables():
    """Create new tables if they don't exist (idempotent)."""
    new_tables = {
        "interviews": """
            CREATE TABLE IF NOT EXISTS interviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL REFERENCES applications(id),
                profile_id INTEGER NOT NULL REFERENCES profiles(id),
                round_number INTEGER DEFAULT 1,
                interview_type VARCHAR(100),
                scheduled_at TIMESTAMP,
                duration_minutes INTEGER,
                interviewer_names TEXT,
                prep_notes TEXT,
                questions_asked TEXT,
                outcome VARCHAR(50),
                feedback TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """,
        "saved_searches": """
            CREATE TABLE IF NOT EXISTS saved_searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL REFERENCES profiles(id),
                name VARCHAR(200),
                query_config TEXT,
                min_score INTEGER DEFAULT 70,
                last_run_at TIMESTAMP,
                results_count INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP
            )
        """,
        "follow_ups": """
            CREATE TABLE IF NOT EXISTS follow_ups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER REFERENCES applications(id),
                profile_id INTEGER NOT NULL REFERENCES profiles(id),
                follow_up_type VARCHAR(50),
                due_date TIMESTAMP NOT NULL,
                completed BOOLEAN DEFAULT 0,
                completed_at TIMESTAMP,
                draft_content TEXT,
                notes TEXT,
                created_at TIMESTAMP
            )
        """,
        "documents": """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL REFERENCES profiles(id),
                job_id INTEGER REFERENCES jobs(id),
                doc_type VARCHAR(50),
                version INTEGER DEFAULT 1,
                title VARCHAR(200),
                content TEXT,
                file_path VARCHAR(500),
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP
            )
        """,
        "contacts": """
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL REFERENCES profiles(id),
                company_id INTEGER REFERENCES companies(id),
                name VARCHAR(200) NOT NULL,
                title VARCHAR(200),
                email VARCHAR(200),
                linkedin_url VARCHAR(500),
                phone VARCHAR(50),
                relationship_type VARCHAR(100),
                notes TEXT,
                last_contacted TIMESTAMP,
                created_at TIMESTAMP
            )
        """,
        "ai_cache": """
            CREATE TABLE IF NOT EXISTS ai_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cache_key VARCHAR(64) UNIQUE,
                response TEXT,
                model_tier VARCHAR(20),
                created_at TIMESTAMP,
                ttl_hours INTEGER DEFAULT 168
            )
        """,
    }

    # PostgreSQL equivalents (SERIAL instead of AUTOINCREMENT)
    new_tables_pg = {
        "interviews": """
            CREATE TABLE IF NOT EXISTS interviews (
                id SERIAL PRIMARY KEY,
                application_id INTEGER NOT NULL REFERENCES applications(id),
                profile_id INTEGER NOT NULL REFERENCES profiles(id),
                round_number INTEGER DEFAULT 1,
                interview_type VARCHAR(100),
                scheduled_at TIMESTAMP,
                duration_minutes INTEGER,
                interviewer_names TEXT,
                prep_notes TEXT,
                questions_asked TEXT,
                outcome VARCHAR(50),
                feedback TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """,
        "saved_searches": """
            CREATE TABLE IF NOT EXISTS saved_searches (
                id SERIAL PRIMARY KEY,
                profile_id INTEGER NOT NULL REFERENCES profiles(id),
                name VARCHAR(200),
                query_config TEXT,
                min_score INTEGER DEFAULT 70,
                last_run_at TIMESTAMP,
                results_count INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP
            )
        """,
        "follow_ups": """
            CREATE TABLE IF NOT EXISTS follow_ups (
                id SERIAL PRIMARY KEY,
                application_id INTEGER REFERENCES applications(id),
                profile_id INTEGER NOT NULL REFERENCES profiles(id),
                follow_up_type VARCHAR(50),
                due_date TIMESTAMP NOT NULL,
                completed BOOLEAN DEFAULT FALSE,
                completed_at TIMESTAMP,
                draft_content TEXT,
                notes TEXT,
                created_at TIMESTAMP
            )
        """,
        "documents": """
            CREATE TABLE IF NOT EXISTS documents (
                id SERIAL PRIMARY KEY,
                profile_id INTEGER NOT NULL REFERENCES profiles(id),
                job_id INTEGER REFERENCES jobs(id),
                doc_type VARCHAR(50),
                version INTEGER DEFAULT 1,
                title VARCHAR(200),
                content TEXT,
                file_path VARCHAR(500),
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP
            )
        """,
        "contacts": """
            CREATE TABLE IF NOT EXISTS contacts (
                id SERIAL PRIMARY KEY,
                profile_id INTEGER NOT NULL REFERENCES profiles(id),
                company_id INTEGER REFERENCES companies(id),
                name VARCHAR(200) NOT NULL,
                title VARCHAR(200),
                email VARCHAR(200),
                linkedin_url VARCHAR(500),
                phone VARCHAR(50),
                relationship_type VARCHAR(100),
                notes TEXT,
                last_contacted TIMESTAMP,
                created_at TIMESTAMP
            )
        """,
        "ai_cache": """
            CREATE TABLE IF NOT EXISTS ai_cache (
                id SERIAL PRIMARY KEY,
                cache_key VARCHAR(64) UNIQUE,
                response TEXT,
                model_tier VARCHAR(20),
                created_at TIMESTAMP,
                ttl_hours INTEGER DEFAULT 168
            )
        """,
    }

    use_pg = not _is_sqlite()
    tables = new_tables_pg if use_pg else new_tables

    with engine.connect() as conn:
        inspector = inspect(engine)
        existing_tables = inspector.get_table_names()
        for table_name, ddl in tables.items():
            if table_name not in existing_tables:
                try:
                    conn.execute(text(ddl))
                    conn.commit()
                    logger.info(f"Migration: created table {table_name}")
                except Exception as e:
                    logger.warning(f"Migration: failed to create table {table_name}: {e}")

        # Create indexes on new tables (idempotent via IF NOT EXISTS or try/except)
        index_stmts = [
            "CREATE INDEX IF NOT EXISTS ix_interviews_application_id ON interviews(application_id)",
            "CREATE INDEX IF NOT EXISTS ix_interviews_profile_id ON interviews(profile_id)",
            "CREATE INDEX IF NOT EXISTS ix_saved_searches_profile_id ON saved_searches(profile_id)",
            "CREATE INDEX IF NOT EXISTS ix_follow_ups_application_id ON follow_ups(application_id)",
            "CREATE INDEX IF NOT EXISTS ix_follow_ups_profile_id ON follow_ups(profile_id)",
            "CREATE INDEX IF NOT EXISTS ix_documents_profile_id ON documents(profile_id)",
            "CREATE INDEX IF NOT EXISTS ix_documents_job_id ON documents(job_id)",
            "CREATE INDEX IF NOT EXISTS ix_contacts_profile_id ON contacts(profile_id)",
            "CREATE INDEX IF NOT EXISTS ix_contacts_company_id ON contacts(company_id)",
            "CREATE INDEX IF NOT EXISTS ix_ai_cache_cache_key ON ai_cache(cache_key)",
            # New composite indexes on existing tables
            "CREATE INDEX IF NOT EXISTS ix_jobs_profile_status_score ON jobs(profile_id, status, match_score)",
            "CREATE INDEX IF NOT EXISTS ix_jobs_created_at ON jobs(created_at)",
        ]
        for stmt in index_stmts:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass  # Index already exists or table not ready


def init_db():
    from backend.models.models import (  # noqa
        Job, Application, Profile, AgentQuestion, Company, User,
        Interview, SavedSearch, FollowUp, Document, Contact, AICache,
    )
    Base.metadata.create_all(bind=engine)
    _run_migrations()
    _create_new_tables()
