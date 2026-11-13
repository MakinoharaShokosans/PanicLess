"""回滚 HTTP 契约：实际文件恢复、冲突/缺失错误和会话租约释放。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from agent_runtime.api.deps import services_of
from agent_runtime.api.errors import install_handlers
from agent_runtime.api.routes.rollback import router
from agent_runtime.runtime.errors import TaskAlreadyRunningError
from tools.builtin.files import FileLimits, WriteFileTool

pytestmark = pytest.mark.unit


@pytest.fixture
async def rig(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "test.txt"
    target.write_text("before")
    record = await WriteFileTool(
        root, FileLimits(100, 4096, 4096), backup_dir=tmp_path / "backups" / "task1"
    ).invoke({"path": "test.txt", "content": "after"})
    task = SimpleNamespace(workspace_id="w1", session_id="s1", permission_level="workspace_write")
    runner = SimpleNamespace(acquire_manual_session=Mock(), release_manual_session=Mock())
    svc = SimpleNamespace(
        home=tmp_path / "os-home",
        backups_dir=tmp_path / "backups",
        tasks=SimpleNamespace(get=AsyncMock(return_value=task), runner=runner),
        memory=SimpleNamespace(
            get_workspace=AsyncMock(return_value=SimpleNamespace(root_path=str(root)))
        ),
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[services_of] = lambda: svc
    install_handlers(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        yield client, target, record.meta["backup"], runner, task


async def test_restore_and_list_are_real(rig):
    client, target, record, runner, _ = rig
    response = await client.get("/api/v1/tasks/task1/backups")
    assert response.status_code == 200 and response.json() == [record]
    restored = await client.post(f"/api/v1/tasks/task1/backups/{record['backup_id']}/restore")
    assert restored.status_code == 200 and restored.json()["status"] == "restored"
    assert target.read_text() == "before"
    runner.acquire_manual_session.assert_called_once_with("s1")
    runner.release_manual_session.assert_called_once_with("s1")


async def test_conflict_and_missing_backup_never_claim_success(rig):
    client, target, record, runner, _ = rig
    target.write_text("newer")
    response = await client.post(f"/api/v1/tasks/task1/backups/{record['backup_id']}/restore")
    assert response.status_code == 409 and response.json()["error"]["code"] == "FILE_CONFLICT"
    response = await client.post("/api/v1/tasks/task1/backups/" + "0" * 32 + "/restore")
    assert response.status_code == 404 and response.json()["error"]["code"] == "BACKUP_NOT_FOUND"
    assert target.read_text() == "newer" and runner.release_manual_session.call_count == 2


async def test_active_task_blocks_rollback(rig):
    client, target, record, runner, _ = rig
    runner.acquire_manual_session.side_effect = TaskAlreadyRunningError("not paused")
    response = await client.post(f"/api/v1/tasks/task1/backups/{record['backup_id']}/restore")
    assert response.status_code == 409
    assert target.read_text() == "after"
    runner.release_manual_session.assert_not_called()


async def test_read_only_task_cannot_restore(rig):
    client, target, record, runner, task = rig
    task.permission_level = "read_only"
    response = await client.post(f"/api/v1/tasks/task1/backups/{record['backup_id']}/restore")
    assert response.status_code == 403
    assert target.read_text() == "after"
    runner.release_manual_session.assert_called_once_with("s1")
