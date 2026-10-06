"""spawn_background: tasks are held until done, failures are logged, shutdown waits."""
import asyncio
import gc
import logging

import pytest

from app.utils import background


@pytest.mark.asyncio
async def test_task_survives_gc_and_is_released_when_done():
    finished = asyncio.Event()

    async def work():
        await asyncio.sleep(0.01)
        finished.set()

    background.spawn_background(work(), name="survivor")
    gc.collect()  # the caller kept no reference
    await asyncio.wait_for(finished.wait(), timeout=1)
    await asyncio.sleep(0)  # let the done callback run
    assert not any(t.get_name() == "survivor" for t in background._tasks)


@pytest.mark.asyncio
async def test_exception_is_logged_with_task_name(caplog):
    async def boom():
        raise RuntimeError("background boom")

    task = background.spawn_background(boom(), name="exploder")
    with caplog.at_level(logging.ERROR, logger="app.utils.background"):
        await asyncio.wait([task])
        await asyncio.sleep(0)
    assert "exploder" in caplog.text
    assert "background boom" in caplog.text
    assert task not in background._tasks


@pytest.mark.asyncio
async def test_wait_background_reports_unfinished(caplog):
    release = asyncio.Event()

    async def slow():
        await release.wait()

    task = background.spawn_background(slow(), name="slowpoke")
    with caplog.at_level(logging.WARNING, logger="app.utils.background"):
        await background.wait_background(timeout=0.01)
    assert "slowpoke" in caplog.text
    release.set()
    await task
