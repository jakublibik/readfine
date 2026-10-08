"""Fire-and-forget tasks that outlive the request which started them.

The event loop keeps only a weak reference to a task, so one nobody holds can be
garbage collected halfway through (the asyncio.create_task docs warn about this), and
an exception from it surfaces only as "Task exception was never retrieved" whenever
the GC gets to it. Every background task goes through spawn_background(), which holds
the task until it is done and logs what it raised.
"""
import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()


def _on_done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Background task %s failed", task.get_name(), exc_info=exc)


def spawn_background(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task:
    """Run coro in the background, holding a reference until it finishes."""
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_on_done)
    return task


async def wait_background(timeout: float) -> None:
    """On shutdown: give running tasks up to timeout seconds, then log what is left.

    What is left is cancelled with the loop. The work is not lost for good: an initial
    fetch is picked up by the scheduler, a pending extraction by the readable queue.
    """
    if not _tasks:
        return
    _done, pending = await asyncio.wait(set(_tasks), timeout=timeout)
    if pending:
        logger.warning(
            "Shutdown left %d background task(s) unfinished: %s",
            len(pending), ", ".join(sorted(t.get_name() for t in pending)),
        )
