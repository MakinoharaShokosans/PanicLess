"""按任务内真实备份身份回滚文件；前端不提交原路径或编造备份名。"""

from __future__ import annotations

import asyncio
from pathlib import Path as FsPath
from typing import Annotated

from fastapi import APIRouter, Depends, Path

from agent_runtime.api.deps import ApiServices, services_of
from agent_runtime.api.errors import ApiError
from tools.builtin.backups import BackupConflictError, list_backups, restore_backup

router = APIRouter(tags=["files"])
Services = Annotated[ApiServices, Depends(services_of)]
TaskId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
BackupId = Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")]


@router.get("/tasks/{task_id}/backups")
async def backups(task_id: TaskId, svc: Services) -> list[dict[str, object]]:
    await svc.tasks.get(task_id)
    try:
        return await asyncio.to_thread(
            list_backups, (svc.backups_dir or svc.home / "backups") / task_id
        )
    except (BackupConflictError, OSError) as exc:
        raise ApiError(409, "BACKUP_UNAVAILABLE", "备份记录不可用") from exc


@router.post("/tasks/{task_id}/backups/{backup_id}/restore")
async def restore(task_id: TaskId, backup_id: BackupId, svc: Services) -> dict[str, object]:
    task = await svc.tasks.get(task_id)
    workspace = await svc.memory.get_workspace(task.workspace_id)
    # 与终端及 Agent 的执行共用会话租约；pause_requested 不等于已暂停。
    svc.tasks.runner.acquire_manual_session(task.session_id)
    try:
        operation = asyncio.create_task(
            asyncio.to_thread(
                restore_backup,
                (svc.backups_dir or svc.home / "backups") / task_id,
                backup_id,
                FsPath(workspace.root_path),
                task.permission_level,
            )
        )
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # 工作线程无法取消，必须等实际写入结束再释放会话租约。
            await operation
            raise
    except FileNotFoundError as exc:
        raise ApiError(404, "BACKUP_NOT_FOUND", "备份不存在") from exc
    except PermissionError as exc:
        raise ApiError(403, "ROLLBACK_FORBIDDEN", str(exc)) from exc
    except BackupConflictError as exc:
        raise ApiError(409, "FILE_CONFLICT", str(exc)) from exc
    except OSError as exc:
        raise ApiError(409, "ROLLBACK_FAILED", "文件恢复失败") from exc
    finally:
        svc.tasks.runner.release_manual_session(task.session_id)
