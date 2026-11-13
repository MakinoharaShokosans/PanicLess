"""真实人工命令和协作式暂停；模型步骤为脚本，shell 使用本地真实进程。"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from agent_runtime.runtime.terminal import RuntimeTerminal
from services.bash_shell.app import ShellSettings, create_app
from tests.helpers.api_support import HEADERS, build_rig
from tools.core import SidecarClient


@pytest.fixture
async def rig(tmp_path):
    async for item in build_rig(tmp_path):
        app = create_app(
            ShellSettings(
                item.services.settings.bash_shell,
                artifacts_dir=tmp_path / "shell-artifacts",
                scratch_dir=tmp_path / "scratch",
                source_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )
        )
        shell = SidecarClient(
            "http://127.0.0.1:8002", timeout_sec=10, transport=httpx.ASGITransport(app=app)
        )
        item.services.terminal = RuntimeTerminal(
            settings=item.services.settings,
            memory=item.services.memory,
            runner=item.services.tasks.runner,
            shell=shell,
            redactor=item.services.redactor,
        )
        try:
            yield item
        finally:
            await shell.aclose()


@pytest.mark.integration
async def test_terminal_returns_real_stdout_exit_code_and_policy_rejection(rig):
    session = await rig.create_session()
    url = f"/api/v1/sessions/{session}/terminal/execute"
    response = await rig.client.post(
        url, json={"command": "printf terminal-real; exit 7"}, headers=HEADERS
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["exit_code"] == 7 and not result["ok"]
    assert "terminal-real" in result["content"]
    denied = await rig.client.post(
        url, json={"command": "touch denied.txt", "permission_level": "read_only"}, headers=HEADERS
    )
    assert denied.status_code == 200
    assert denied.json()["result"]["meta"]["policy_rejected"]
    assert not (rig.root / "denied.txt").exists()
    assert (await rig.client.post(url, json={"command": "pwd"})).status_code == 401


@pytest.mark.integration
async def test_terminal_reports_real_timeout(rig):
    session = await rig.create_session()
    response = await rig.client.post(
        f"/api/v1/sessions/{session}/terminal/execute",
        json={"command": "sleep 5", "timeout_sec": 0.05},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result["ok"] and result["meta"]["timed_out"]


@pytest.mark.integration
async def test_control_must_reach_paused_before_manual_execution(rig):
    rig.world.gate = asyncio.Event()
    submitted = await rig.submit()
    task_id, session = submitted["task_id"], submitted["session_id"]
    await rig.world.entered.wait()
    control = f"/api/v1/tasks/{task_id}/control"
    response = await rig.client.post(control, json={"action": "pause"}, headers=HEADERS)
    assert response.status_code == 202 and response.json()["control_state"] == "pause_requested"
    terminal = f"/api/v1/sessions/{session}/terminal/execute"
    blocked = await rig.client.post(terminal, json={"command": "pwd"}, headers=HEADERS)
    assert blocked.status_code == 409
    rig.world.gate.set()
    runner = rig.services.tasks.runner
    await asyncio.wait_for(runner._records[task_id].paused.wait(), 2)
    assert (await rig.client.get(control, headers=HEADERS)).json()["control_state"] == "paused"
    actual = await rig.client.post(terminal, json={"command": "pwd"}, headers=HEADERS)
    assert actual.status_code == 200 and actual.json()["result"]["ok"]
    resumed = await rig.client.post(control, json={"action": "resume"}, headers=HEADERS)
    assert resumed.status_code == 202 and resumed.json()["control_state"] == "autonomous"
    assert (await runner.wait(task_id)).status == "partial"
