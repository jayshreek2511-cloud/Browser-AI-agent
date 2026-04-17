from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from app.agent.orchestrator import ResearchOrchestrator, task_runner
from app.core.database import get_session
from app.models.schemas import TaskCreateRequest
from app.storage.repositories import TaskRepository

router = APIRouter()
orchestrator = ResearchOrchestrator()


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/ui/index.html")


@router.post("/api/tasks")
async def create_task(
    payload: TaskCreateRequest,
    session: AsyncSession = Depends(get_session),
):
    repo = TaskRepository(session)
    task = await repo.create_task(payload.query)
    background_session = AsyncSession(session.bind)
    runner = asyncio.create_task(orchestrator.run(background_session, task.id))
    task_runner.track(task.id, runner)
    return {"task_id": task.id}


@router.get("/api/tasks")
async def list_tasks(session: AsyncSession = Depends(get_session)):
    repo = TaskRepository(session)
    return await repo.build_task_summaries()


@router.get("/api/tasks/{task_id}")
async def get_task(task_id: str, session: AsyncSession = Depends(get_session)):
    repo = TaskRepository(session)
    task = await repo.build_task_detail(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.get("/artifacts/{path:path}")
async def get_artifact(path: str):
    file_path = Path("artifacts") / path
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(file_path)
