"""界面配置真实保存、重启读取、Agent 装配和 Shell 请求接线。"""

from pathlib import Path

import httpx
import pytest

from agent_runtime.api import create_app
from agent_runtime.bootstrap import Application
from agent_runtime.core import new_state
from baseagent_config import load_settings
from tests.helpers.api_support import HEADERS, TOKEN
from tools.core import SidecarClient

pytestmark = pytest.mark.integration


async def test_operator_settings_persist_and_reach_agent(tmp_path: Path, config_path: Path):
    loaded = load_settings(
        config_path,
        {"app": {"home": str(tmp_path / "home")}},
        require_models=False,
    )
    app = await Application.create(loaded)
    requests = []

    def reply(request):
        import json

        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "SUCCESS", "exit_code": 0})

    shell = SidecarClient(
        "http://127.0.0.1:8002", timeout_sec=5, transport=httpx.MockTransport(reply)
    )
    app.factory.shell = shell
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(app.api_services(), token=TOKEN)),
            base_url="http://127.0.0.1:8000",
            headers=HEADERS,
        ) as client:
            defaults = (await client.get("/api/v1/settings/tools")).json()
            assert defaults == {"bash_max_timeout_sec": 600, "bash_output_max_bytes": 8192}
            values = {"bash_max_timeout_sec": 3, "bash_output_max_bytes": 2048}
            assert (await client.put("/api/v1/settings/tools", json=values)).json() == values
            invalid = {**values, "bash_max_timeout_sec": 601}
            assert (await client.put("/api/v1/settings/tools", json=invalid)).status_code == 422
            assert (await client.get("/api/v1/settings/tools")).json() == values
            assert (await client.get("/api/v1/settings/rules")).json() == {"content": ""}
            rules = {"content": "维护窗口：周六 03:00；变更先检查配置语法。"}
            assert (await client.put("/api/v1/settings/rules", json=rules)).json() == rules
            await app.memory.create_workspace("ops", "ops", str(tmp_path))
            await app.memory.create_session("session", "ops")
            state = new_state(
                workspace_id="ops",
                workspace_path=str(tmp_path),
                session_id="session",
                task_id="test",
                task_goal="检查",
                permission_level="read_only",
                canary_token="test-canary",
            )
            assembled = await app.factory._assemble(state)
            assert rules["content"] in assembled.assets.system
            bash = assembled.registry.get("bash")
            assert bash is not None
            await bash.invoke({"command": "pwd", "timeout_sec": 100})
            assert requests[-1]["timeout_sec"] == 3
            assert requests[-1]["output_max_bytes"] == 2048
            await client.put("/api/v1/settings/rules", json={"content": ""})
            assert rules["content"] in assembled.assets.system  # 在途任务快照不变
            assert rules["content"] not in (await app.factory._assemble(state)).assets.system
    finally:
        await shell.aclose()
        await app.aclose()

    reopened = await Application.create(loaded)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(reopened.api_services(), token=TOKEN)),
            base_url="http://127.0.0.1:8000",
            headers=HEADERS,
        ) as client:
            assert (await client.get("/api/v1/settings/tools")).json() == values
            assert (await client.get("/api/v1/settings/rules")).json() == {"content": ""}
    finally:
        await reopened.aclose()
