"""人工命令执行：复用 BashTool、权限判定与 sidecar，不模拟交互式 PTY。"""

from __future__ import annotations

import asyncio
import uuid

from agent_runtime.core import ToolResult
from agent_runtime.guardrails import check_permission
from agent_runtime.memory import MemoryStore
from agent_runtime.observability import Redactor
from agent_runtime.runtime.assembly import permission_policy_from_settings, resolve_permission_level
from agent_runtime.runtime.operator_settings import load_tool_settings
from agent_runtime.runtime.overrides import load_workspace_settings
from agent_runtime.runtime.runner import TaskRunner
from baseagent_config import Settings
from tools.builtin.shell import BashTool
from tools.core import SidecarClient


class RuntimeTerminal:
    def __init__(
        self,
        *,
        settings: Settings,
        memory: MemoryStore,
        runner: TaskRunner,
        shell: SidecarClient,
        redactor: Redactor,
    ) -> None:
        self.settings = settings
        self.memory = memory
        self.runner = runner
        self.shell = shell
        self.redactor = redactor

    async def execute(
        self,
        session_id: str,
        command: str,
        *,
        timeout_sec: float | None = None,
        permission_level: str | None = None,
        confirm_full_permissions: bool = False,
    ) -> dict[str, object]:
        session = await self.memory.get_session(session_id)
        workspace = await self.memory.get_workspace(session.workspace_id)
        workspace_settings = await load_workspace_settings(self.memory, workspace.workspace_id)
        level = resolve_permission_level(
            self.settings,
            permission_level or workspace_settings.permission_level,
            confirmed=confirm_full_permissions,
        )
        policy = permission_policy_from_settings(self.settings)
        verdict = check_permission(policy, level, "bash", {"command": command})
        command_id = "terminal_" + uuid.uuid4().hex[:12]
        if not verdict.allowed:
            result = ToolResult.failure(
                verdict.reason,
                meta={"policy_rejected": True, "required_level": verdict.required_level},
            )
        else:
            tools = await load_tool_settings(self.memory, self.settings.bash_shell)
            shell = BashTool(
                self.shell,
                workspace_id=workspace.workspace_id,
                workspace_root=workspace.root_path,
                task_id=command_id,
                default_timeout_sec=min(
                    self.settings.bash_shell.timeout_sec, tools.bash_max_timeout_sec
                ),
                max_timeout_sec=tools.bash_max_timeout_sec,
                output_max_bytes=tools.bash_output_max_bytes,
            )
            self.runner.acquire_manual_session(session_id)
            pending = asyncio.create_task(
                shell.invoke({"command": command, "timeout_sec": timeout_sec})
            )
            try:
                try:
                    result = await asyncio.shield(pending)
                except asyncio.CancelledError:
                    # 浏览器断开时仍等到 sidecar 的限时执行结束，避免过早放行 Agent。
                    await pending
                    raise
            finally:
                self.runner.release_manual_session(session_id)
        raw = result.model_dump(mode="json")
        return {
            "command_id": command_id,
            "session_id": session_id,
            "workspace_id": workspace.workspace_id,
            "mode": "non_interactive",
            "result": self.redactor.value(raw),
        }
