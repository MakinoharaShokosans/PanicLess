"""人工终端命令与任务协作式暂停；实际状态由运行器返回。"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.api.deps import ApiServices, services_of
from agent_runtime.api.errors import ApiError

router = APIRouter(tags=["terminal"])
Services = Annotated[ApiServices, Depends(services_of)]
TaskId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
SessionId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class TerminalCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    command: str = Field(min_length=1, max_length=20000)
    timeout_sec: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    permission_level: Literal["read_only", "workspace_write", "full_permissions"] | None = None
    confirm_full_permissions: bool = False


class TaskControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["pause", "resume"]


@router.post("/sessions/{session_id}/terminal/execute")
async def execute(session_id: SessionId, body: TerminalCommand, svc: Services) -> dict[str, object]:
    if svc.terminal is None:
        raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "人工终端未装配")
    return await svc.terminal.execute(session_id, **body.model_dump())


@router.get("/tasks/{task_id}/control")
async def get_control(task_id: TaskId, svc: Services) -> dict[str, str]:
    view = await svc.tasks.get(task_id)
    if view.status != "running":
        return {"task_id": task_id, "status": view.status, "control_state": "inactive"}
    return svc.tasks.runner.control_state(task_id)


@router.post("/tasks/{task_id}/control", status_code=202)
async def control(task_id: TaskId, body: TaskControl, svc: Services) -> dict[str, str]:
    await svc.tasks.get(task_id)
    result = svc.tasks.runner.control(task_id, body.action)
    await svc.hub.emit(task_id, "task.control", result)
    return result
