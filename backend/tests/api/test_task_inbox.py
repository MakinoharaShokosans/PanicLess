"""运行中消息入口，真实 SQLite + 脚本模型。"""

import asyncio

import pytest

from tests.helpers.api_support import HEADERS, build_rig


@pytest.fixture
async def rig(tmp_path):
    async for item in build_rig(tmp_path):
        yield item


@pytest.mark.integration
async def test_task_message_api_preserves_task_and_tracks_consumption(rig):
    rig.world.gate = asyncio.Event()
    task = await rig.submit()
    task_id = task["task_id"]
    await rig.world.entered.wait()
    url = f"/api/v1/tasks/{task_id}/messages"
    assert (await rig.client.post(url, json={"content": "hello"})).status_code == 401
    assert (await rig.client.post(url, json={"content": " "}, headers=HEADERS)).status_code == 400
    added = await rig.client.post(url, json={"content": "端口改为 8081"}, headers=HEADERS)
    assert added.status_code == 202, added.text
    assert added.json()["task_id"] == task_id
    queued = await rig.client.get(url, headers=HEADERS)
    assert queued.json()[0]["status"] == "queued"
    rig.world.gate.set()
    await rig.services.tasks.runner.wait(task_id)
    consumed = await rig.client.get(url, headers=HEADERS)
    assert consumed.json()[0]["status"] == "consumed"
    snapshot = await rig.client.get(f"/api/v1/tasks/{task_id}", headers=HEADERS)
    assert snapshot.json()["task_goal"] == task["task_goal"]
    late = await rig.client.post(url, json={"content": "late"}, headers=HEADERS)
    assert late.status_code == 409
    assert (
        await rig.client.get("/api/v1/tasks/missing/messages", headers=HEADERS)
    ).status_code == 404
