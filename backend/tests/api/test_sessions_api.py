"""会话管理 API 契约测试（无显式工作区模式与会话生命周期）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.api_support import HEADERS, ApiRig, build_rig

V1 = "/api/v1"


@pytest.fixture
async def rig(tmp_path: Path):  # type: ignore[no-untyped-def]
    async for r in build_rig(tmp_path):
        yield r


@pytest.mark.unit
async def test_zero_workspace_session_flow(rig: ApiRig) -> None:
    # 1. POST /sessions: 验证无工作区会话创建
    empty_res = await rig.client.post(f"{V1}/sessions", headers=HEADERS)
    assert empty_res.status_code == 201
    assert empty_res.json()["session_id"].startswith("sess_")

    response = await rig.client.post(
        f"{V1}/sessions", json={"title": "运维排障"}, headers=HEADERS
    )
    assert response.status_code == 201
    data = response.json()
    sid = data["session_id"]
    wid = data["workspace_id"]
    assert sid.startswith("sess_")
    assert isinstance(wid, str) and wid
    assert data["title"] == "运维排障"

    # 2. 验证物理目录在 storage/workspaces/<session_id> 自动生成
    expected_dir = rig.services.workspaces_path / sid
    assert expected_dir.is_dir()

    # 验证关联工作区元数据
    ws_record = await rig.services.memory.get_workspace(wid)
    assert ws_record.root_path == str(expected_dir.resolve())
    assert ws_record.name == f"Session {sid}"

    # 3. 验证 GET /sessions 列表可见
    list_res = await rig.client.get(f"{V1}/sessions", headers=HEADERS)
    assert list_res.status_code == 200
    all_sids = [s["session_id"] for s in list_res.json()]
    assert sid in all_sids

    # 4. 验证 DELETE /sessions 级联删除物理目录并返回 204
    del_res = await rig.client.delete(f"{V1}/sessions/{sid}", headers=HEADERS)
    assert del_res.status_code == 204
    assert not expected_dir.exists()
    assert not await rig.services.memory.workspace_exists(wid)
    assert not await rig.services.memory.session_exists(sid)
