"""Background task manager for non-blocking AI endpoints."""
import asyncio
import logging
import uuid
from datetime import datetime
from collections import OrderedDict

logger = logging.getLogger(__name__)

# In-memory task store: {task_id: {status, result, error, started_at, completed_at, task_type, profile_id}}
_tasks: OrderedDict[str, dict] = OrderedDict()
MAX_COMPLETED = 50


def _prune():
    """Remove oldest completed tasks when we exceed MAX_COMPLETED."""
    completed = [tid for tid, t in _tasks.items() if t["status"] in ("completed", "failed")]
    while len(completed) > MAX_COMPLETED:
        oldest = completed.pop(0)
        _tasks.pop(oldest, None)


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
    _tasks[task_id] = {
        "status": "running",
        "result": None,
        "error": None,
        "started_at": datetime.utcnow().isoformat(),
        "completed_at": None,
        "task_type": task_type,
        "profile_id": profile_id,
    }

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
            _prune()

    asyncio.get_event_loop().create_task(_wrapper())
    return task_id


def get_task_status(task_id: str) -> dict | None:
    """Return the task dict for a given task_id, or None if not found."""
    return _tasks.get(task_id)


def find_running_task(task_type: str, profile_id: int) -> str | None:
    """Find a currently-running task of the given type for a profile.

    This prevents duplicate background tasks from being launched.
    Returns the task_id if found, else None.
    """
    for tid, t in _tasks.items():
        if t["task_type"] == task_type and t["profile_id"] == profile_id and t["status"] == "running":
            return tid
    return None
