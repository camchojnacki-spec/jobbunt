"""Background task manager for non-blocking AI endpoints.

Uses an in-memory OrderedDict as a fast cache, with database persistence
so that task state survives Cloud Run redeployments and multi-instance routing.
"""
import asyncio
import inspect
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
    completed = [tid for tid, t in _tasks.items() if t["status"] in ("completed", "failed", "cancelled")]
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
            # Inject task_id so background functions can check cancellation
            # Only inject if the function signature accepts it
            sig = inspect.signature(func)
            if "task_id" in sig.parameters:
                result = await func(*args, task_id=task_id, **kwargs)
            else:
                result = await func(*args, **kwargs)
            _tasks[task_id]["result"] = result
            # Only transition to completed if not already cancelled/failed by timeout
            if _tasks[task_id]["status"] == "running":
                _tasks[task_id]["status"] = "completed"
        except Exception as e:
            logger.error(f"Background task {task_id} ({task_type}) failed: {e}", exc_info=True)
            if _tasks[task_id]["status"] == "running":
                _tasks[task_id]["error"] = f"{type(e).__name__}: {str(e)[:500]}"
                _tasks[task_id]["status"] = "failed"
        finally:
            if not _tasks[task_id].get("completed_at"):
                _tasks[task_id]["completed_at"] = datetime.utcnow().isoformat()
            # Persist final state to DB
            _persist_task(task_id, _tasks[task_id])
            _prune()

    asyncio.get_event_loop().create_task(_wrapper())
    return task_id


def get_task_status(task_id: str) -> dict | None:
    """Return the task dict for a given task_id.

    Checks in-memory cache first, then falls back to database lookup.
    Auto-fails tasks stuck in 'running' for more than 5 minutes.
    """
    # Fast path: in-memory cache
    cached = _tasks.get(task_id)
    if cached is not None:
        task = cached
    else:
        # Slow path: database fallback (handles cross-instance and post-deploy lookups)
        task = _load_task_from_db(task_id)

    # Auto-timeout: fail tasks stuck in 'running' for more than 5 minutes
    if task and task["status"] == "running" and task.get("started_at"):
        try:
            started = datetime.fromisoformat(task["started_at"])
            elapsed = (datetime.utcnow() - started).total_seconds()
            if elapsed > 300:
                logger.warning(f"Task {task_id} timed out after {elapsed:.0f}s — auto-failing")
                task["status"] = "failed"
                task["error"] = "Task timed out after 5 minutes"
                task["completed_at"] = datetime.utcnow().isoformat()
                _persist_task(task_id, task)
        except (ValueError, TypeError):
            pass

    return task


def cancel_task(task_id: str) -> dict | None:
    """Cancel a running task. Returns the updated task dict, or None if not found."""
    task = get_task_status(task_id)
    if task is None:
        return None

    if task["status"] != "running":
        return task  # Already finished, nothing to cancel

    logger.info(f"Cancelling task {task_id}")
    task["status"] = "cancelled"
    task["error"] = "Cancelled by user"
    task["completed_at"] = datetime.utcnow().isoformat()
    _tasks[task_id] = task
    _persist_task(task_id, task)
    return task


def is_task_cancelled(task_id: str) -> bool:
    """Check if a task has been cancelled. Used by background workers to abort early."""
    task = _tasks.get(task_id)
    if task and task["status"] == "cancelled":
        return True
    # Also check DB in case cancellation happened on another instance
    try:
        from backend.database import SessionLocal
        from backend.models.models import BackgroundTask

        db = SessionLocal()
        try:
            row = db.query(BackgroundTask).filter(BackgroundTask.id == task_id).first()
            if row and row.status == "cancelled":
                # Sync to in-memory cache
                if task_id in _tasks:
                    _tasks[task_id]["status"] = "cancelled"
                return True
        finally:
            db.close()
    except Exception:
        pass
    return False


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
            # Check if this task has timed out (get_task_status handles auto-fail)
            refreshed = get_task_status(tid)
            if refreshed and refreshed["status"] == "running":
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
                # Cache it locally, then check timeout via get_task_status
                refreshed = get_task_status(row.id)
                if refreshed and refreshed["status"] == "running":
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
