"""Background task manager for non-blocking AI endpoints.

Uses an in-memory OrderedDict as a fast cache, with database persistence
so that task state survives Cloud Run redeployments and multi-instance routing.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime
from collections import OrderedDict

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# In-memory task store (fast cache): {task_id: {status, result, error, started_at, completed_at, task_type, profile_id}}
_tasks: OrderedDict[str, dict] = OrderedDict()
MAX_COMPLETED = 50


def _prune():
    """Remove oldest completed tasks when we exceed MAX_COMPLETED."""
    completed = [tid for tid, t in _tasks.items() if t["status"] in ("completed", "failed")]
    while len(completed) > MAX_COMPLETED:
        oldest = completed.pop(0)
        _tasks.pop(oldest, None)


def _persist_task(task_id: str, task_data: dict):
    """Write task state to the database (fire-and-forget, non-blocking for the caller)."""
    try:
        from backend.database import SessionLocal
        from backend.models.models import BackgroundTask

        db: Session = SessionLocal()
        try:
            row = db.query(BackgroundTask).filter(BackgroundTask.id == task_id).first()
            if row is None:
                row = BackgroundTask(
                    id=task_id,
                    task_type=task_data.get("task_type", "unknown"),
                    profile_id=task_data.get("profile_id"),
                    status=task_data["status"],
                    started_at=datetime.fromisoformat(task_data["started_at"]) if task_data.get("started_at") else datetime.utcnow(),
                )
                db.add(row)
            else:
                row.status = task_data["status"]

            # Serialize result to JSON if it's not a string
            result = task_data.get("result")
            if result is not None:
                row.result = json.dumps(result) if not isinstance(result, str) else result
            else:
                row.result = None

            row.error = task_data.get("error")

            if task_data.get("completed_at"):
                row.completed_at = datetime.fromisoformat(task_data["completed_at"])

            db.commit()
        except Exception as e:
            logger.warning(f"Failed to persist task {task_id} to DB: {e}")
            db.rollback()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Failed to persist task {task_id} (session setup): {e}")


def run_background(task_type: str, profile_id: int, func, *args, **kwargs) -> str:
    """Launch an async function as a background task and return the task_id immediately.

    Args:
        task_type: descriptive label like "career-stats", "scouting-report", etc.
        profile_id: the profile this task is for
        func: an async callable
        *args, **kwargs: forwarded to func

    Returns:
        task_id (str) that can be polled with get_task_status().
    """
    task_id = str(uuid.uuid4())
    task_data = {
        "status": "running",
        "result": None,
        "error": None,
        "started_at": datetime.utcnow().isoformat(),
        "completed_at": None,
        "task_type": task_type,
        "profile_id": profile_id,
    }
    _tasks[task_id] = task_data

    # Persist initial "running" state to DB
    _persist_task(task_id, task_data)

    async def _wrapper():
        try:
            result = await func(*args, **kwargs)
            _tasks[task_id]["result"] = result
            _tasks[task_id]["status"] = "completed"
        except Exception as e:
            logger.error(f"Background task {task_id} ({task_type}) failed: {e}", exc_info=True)
            _tasks[task_id]["error"] = f"{type(e).__name__}: {str(e)[:500]}"
            _tasks[task_id]["status"] = "failed"
        finally:
            _tasks[task_id]["completed_at"] = datetime.utcnow().isoformat()
            # Persist final state to DB
            _persist_task(task_id, _tasks[task_id])
            _prune()

    asyncio.get_event_loop().create_task(_wrapper())
    return task_id


def get_task_status(task_id: str) -> dict | None:
    """Return the task dict for a given task_id.

    Checks in-memory cache first, then falls back to database lookup.
    """
    # Fast path: in-memory cache
    cached = _tasks.get(task_id)
    if cached is not None:
        return cached

    # Slow path: database fallback (handles cross-instance and post-deploy lookups)
    return _load_task_from_db(task_id)


def _load_task_from_db(task_id: str) -> dict | None:
    """Load a task from the database and populate the in-memory cache."""
    try:
        from backend.database import SessionLocal
        from backend.models.models import BackgroundTask

        db: Session = SessionLocal()
        try:
            row = db.query(BackgroundTask).filter(BackgroundTask.id == task_id).first()
            if row is None:
                return None

            # Deserialize result from JSON
            result = None
            if row.result is not None:
                try:
                    result = json.loads(row.result)
                except (json.JSONDecodeError, TypeError):
                    result = row.result

            task_data = {
                "status": row.status,
                "result": result,
                "error": row.error,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "completed_at": row.completed_at.isoformat() if row.completed_at else None,
                "task_type": row.task_type,
                "profile_id": row.profile_id,
            }

            # Cache it in memory for subsequent polls
            _tasks[task_id] = task_data
            return task_data
        except Exception as e:
            logger.warning(f"Failed to load task {task_id} from DB: {e}")
            return None
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Failed to load task {task_id} (session setup): {e}")
        return None


def find_running_task(task_type: str, profile_id: int) -> str | None:
    """Find a currently-running task of the given type for a profile.

    Checks in-memory cache first, then falls back to database.
    This prevents duplicate background tasks from being launched.
    Returns the task_id if found, else None.
    """
    # Check in-memory first
    for tid, t in _tasks.items():
        if t["task_type"] == task_type and t["profile_id"] == profile_id and t["status"] == "running":
            return tid

    # Fallback: check database for running tasks on other instances
    try:
        from backend.database import SessionLocal
        from backend.models.models import BackgroundTask

        db: Session = SessionLocal()
        try:
            row = db.query(BackgroundTask).filter(
                BackgroundTask.task_type == task_type,
                BackgroundTask.profile_id == profile_id,
                BackgroundTask.status == "running",
            ).first()
            if row:
                # Cache it locally
                _load_task_from_db(row.id)
                return row.id
            return None
        except Exception as e:
            logger.warning(f"Failed to query running tasks from DB: {e}")
            return None
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Failed to query running tasks (session setup): {e}")
        return None
