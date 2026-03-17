"""Database setup and session management."""
import os
import logging
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker, DeclarativeBase

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "jobbunt.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _run_migrations():
    """Add any new columns to existing tables (SQLite doesn't auto-add them)."""
    inspector = inspect(engine)
    migrations = [
        ("profiles", "advisor_data", "TEXT"),
        ("profiles", "user_id", "INTEGER REFERENCES users(id)"),
        ("profiles", "career_history", "TEXT"),
        ("profile_questions", "purpose", "TEXT"),
    ]
    with engine.connect() as conn:
        for table, column, col_type in migrations:
            existing = [c["name"] for c in inspector.get_columns(table)]
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
