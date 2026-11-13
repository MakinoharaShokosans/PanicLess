"""``spawn_subagent``：动态委派。设计文档：docs/06_subagents/01_dynamic_delegation.md §2-3、§5.3。

入参是主模型生成的**提议**，不是授权。派发前一次性完成三道收窄（工具集全有或全无、权限不升级、
深度），之后子级面对的是一个已经定死的世界。返回值恒标 ``untrusted``；拿到结构良好的报告就算成功。
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from agent_runtime.core import AgentTool, ToolResult
from agent_runtime.subagents.delegate.render import render_report
from agent_runtime.subagents.delegate.runner import SubagentRunner
from agent_runtime.subagents.delegate.store import ChildConversation, ChildStore
from agent_runtime.subagents.usage import ChildUsage
from tools.core import ToolRegistry

_MAX_ROLE = 120
_MAX_GOAL = 1200
_DEADLINE_MARGIN_SEC = 15.0


class SpawnSubagentTool(AgentTool):
    name = "spawn_subagent"
    description = (
        "临时组建一个受限的子劳动力，去完成一个需要阅读大量材料、但只需回流简短结论的子任务"
        "（例如通读十几个文件找出某个调用链）。你必须明确列出分配给它的工具；它的权限不会高于你，"
        "遇到需要审批的操作会直接失败。回流的是带引用的结论，可能失真，请用本地证据交叉核对。"
        "返回 subtask_id；继续调查时传 subtask_id 和 goal 续问同一个子任务，沿用原工具授权。"
        "超时或提炼失败仍返回中间发现和原始记录的 artifact_id。"
    )
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "subtask_id": {"type": "string", "description": "续问已有子任务时填写返回的 ID"},
            "role": {"type": "string", "description": "子劳动力的角色代号，如 code_reader"},
            "goal": {"type": "string", "description": "它要完成的具体目标"},
            "assigned_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "分配给它的工具名，必须是你当前拥有的工具的子集",
            },
            "max_steps": {"type": "integer", "description": "可选，步数上限（受配置上限约束）"},
        },
        "required": ["goal"],
    }
    #: 接口本身可信；每次返回时在结果上覆盖为 untrusted
    trust = "trusted"
    idempotent = False

    def __init__(
        self,
        runner_factory: Callable[[ToolRegistry], SubagentRunner],
        parent_tools: Callable[[], Mapping[str, AgentTool]],
        *,
        enabled: bool,
        depth: int,
        max_depth: int,
        denied_tools: Collection[str],
        max_assigned_tools: int,
        max_steps: int,
        max_wall_time_sec: float,
        max_report_chars: int,
        storage_dir: Path | None = None,
    ) -> None:
        self._store = ChildStore(storage_dir) if storage_dir is not None else None
        self._factory = runner_factory
        self._parent_tools = parent_tools
        self._enabled = enabled
        self._depth = depth
        self._max_depth = max_depth
        self._denied = set(denied_tools) | {self.name, "report_outcome", "record_verification"}
        self._max_assigned = max_assigned_tools
        self._max_steps = max_steps
        self._max_wall = max_wall_time_sec
        self._max_report = max_report_chars
        self.timeout_sec = max_wall_time_sec + _DEADLINE_MARGIN_SEC

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult:
        if not self._enabled:
            return ToolResult.failure("动态委派未启用，请自行完成")
        if self._depth + 1 > self._max_depth:
            return ToolResult.failure("已达到委派深度上限，不能再委派；请自行完成")
        goal = args.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            return ToolResult.failure("缺少参数 goal（子任务目标或续问）")
        subtask_id = args.get("subtask_id")
        if subtask_id is not None and not isinstance(subtask_id, str):
            return ToolResult.failure("subtask_id 必须是字符串")
        if subtask_id is not None and self._store is None:
            return ToolResult.failure("未配置子任务存储，不能续问")
        try:
            if subtask_id is not None:
                assert self._store is not None
                lease = self._store.lease(subtask_id)
            else:
                lease = nullcontext()
            with lease:
                if subtask_id is not None:
                    assert self._store is not None
                    child = self._store.load(subtask_id)
                    if "assigned_tools" in args and args["assigned_tools"] != child.assigned_tools:
                        return ToolResult.failure("续问沿用原工具授权，不能修改 assigned_tools")
                    if "role" in args and args["role"] != child.role:
                        return ToolResult.failure("续问沿用原角色，不能修改 role")
                    narrowed = self._narrow(child.assigned_tools)
                else:
                    role = args.get("role")
                    if not isinstance(role, str) or not role.strip():
                        return ToolResult.failure("新建子任务需要 role（角色代号）")
                    narrowed = self._narrow(args.get("assigned_tools"))
                    child = ChildConversation.create(
                        role.strip()[:_MAX_ROLE],
                        [t.name for t in narrowed] if isinstance(narrowed, list) else [],
                    )
                if isinstance(narrowed, str):
                    return ToolResult.failure(narrowed)
                if subtask_id is None and self._store is not None:
                    with self._store.lease(child.subtask_id):
                        return await self._run(child, narrowed, goal, args)
                return await self._run(child, narrowed, goal, args)
        except FileNotFoundError:
            return ToolResult.failure("未找到该子任务；只能续问当前父任务创建的子任务")
        except ValueError as exc:
            return ToolResult.failure(str(exc))

    async def _run(
        self, child: ChildConversation, tools: list[AgentTool], goal: str, args: Mapping[str, Any]
    ) -> ToolResult:
        usage = ChildUsage(self._max_wall)
        runner = self._factory(ToolRegistry(tools, allow_untrusted=True))
        report = await runner.run(
            child.role,
            goal.strip()[:_MAX_GOAL],
            usage,
            self._steps_from(args),
            conversation=child,
            store=self._store,
        )
        artifact_id = child.subtask_id if self._store is not None else None
        artifact_path = (
            str(self._store.evidence_path(child.subtask_id)) if self._store is not None else None
        )
        header = f"子任务 ID：{child.subtask_id}（续问传 subtask_id 与 goal）\n"
        if artifact_id:
            header += f"完整会话与原始观察：artifact://{artifact_id}\n"
        return ToolResult(
            ok=True,  # A partial/blocked investigation is a valid report, not a tool crash.
            content=header + render_report(report, role=child.role, max_chars=self._max_report),
            artifact_id=artifact_id,
            artifact_path=artifact_path,
            meta={
                "subtask_id": child.subtask_id,
                "status": report.status,
                "steps": report.steps_used,
                "tool_calls": report.tool_calls,
                "tokens": usage.used,
            },
            trust="untrusted",
        )

    def _narrow(self, requested: object) -> list[AgentTool] | str:
        """工具集收窄：全有或全无，不做静默收窄。"""
        if not isinstance(requested, list) or not requested:
            return "assigned_tools 不能为空：不提供默认授权，不给就是没有"
        names = [n for n in requested if isinstance(n, str)]
        if len(names) != len(requested):
            return "assigned_tools 必须是工具名字符串列表"
        unique = list(dict.fromkeys(names))
        parent = self._parent_tools()
        grantable = sorted(n for n in parent if n not in self._denied)
        bad = [n for n in unique if n not in parent or n in self._denied]
        if bad:
            return f"这些工具不能分配：{bad}。当前可授权的工具：{grantable}"
        if len(unique) > self._max_assigned:
            return f"分配的工具超过上限 {self._max_assigned} 个。当前可授权的工具：{grantable}"
        return [parent[n] for n in unique]

    def _steps_from(self, args: Mapping[str, Any]) -> int:
        asked = args.get("max_steps")
        if isinstance(asked, int) and not isinstance(asked, bool) and asked > 0:
            return min(asked, self._max_steps)
        return self._max_steps
