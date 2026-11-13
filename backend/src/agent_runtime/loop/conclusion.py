"""目标结论与显式验证：模型提交判断，运行时核对证据引用和未完成工作。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from agent_runtime.core import AgentState, AgentTool, ToolResult
from agent_runtime.core.state import TaskConclusion, VerificationRecord

ASSESSMENT_TOOLS = frozenset({"record_verification", "report_outcome"})


class RecordVerificationTool(AgentTool):
    name = "record_verification"
    description = (
        "记录与任务目标对应的验证：goal 是检查对应的目标，check 是实际检查内容，"
        "summary 是观察结果，evidence_call_ids 引用已执行工具调用。普通 Bash 成功不等于修复成功。"
        "仅将直接证明该目标的证据标为 passed；证据不充分用 inconclusive。修改后须重新验证。"
    )
    idempotent = True
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "goal": {"type": "string"},
            "check": {"type": "string"},
            "summary": {"type": "string"},
            "result": {"type": "string", "enum": ["passed", "failed", "inconclusive"]},
            "evidence_call_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        "required": ["goal", "check", "summary", "result", "evidence_call_ids"],
        "additionalProperties": False,
    }

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult:
        return ToolResult.failure("由工具循环处理")


class ReportOutcomeTool(AgentTool):
    name = "report_outcome"
    description = (
        "最终回复前报告原任务目标完成情况：completed 全部完成；partial 部分完成；"
        "needs_input 缺少用户信息；blocked 被环境或依赖阻断。summary说明依据，remaining列未完成项。"
        "有未完成计划或改动未验证时不能报completed。报告后直接给用户最终结论。"
    )
    idempotent = True
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["completed", "partial", "needs_input", "blocked"],
            },
            "summary": {"type": "string"},
            "remaining": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["status", "summary", "remaining"],
        "additionalProperties": False,
    }

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult:
        return ToolResult.failure("由工具循环处理")


def assess(
    state: AgentState, name: str, args: Mapping[str, Any]
) -> tuple[dict[str, object], str, bool]:
    try:
        if name == "record_verification":
            record = VerificationRecord.model_validate({**args, "revision": state.work_revision})
            if not all(t.strip() for t in (record.goal, record.check, record.summary)):
                raise ValueError("goal/check/summary 不得为空")
            observations = {o.call_id: o for o in state.observations}
            if not record.evidence_call_ids or any(
                i not in observations for i in record.evidence_call_ids
            ):
                raise ValueError("必须引用真实已执行调用的 evidence_call_ids")
            evidence = [observations[i] for i in record.evidence_call_ids]
            if any(o.revision != state.work_revision for o in evidence):
                raise ValueError("证据早于最近一次改动，请重新检查")
            if any(o.changed_state or o.tool in {"write_file", "edit_file"} for o in evidence):
                raise ValueError("改动本身不是验证，请引用后续检查调用")
            if record.result == "passed" and any(not o.ok for o in evidence):
                raise ValueError("失败的工具结果不能作为通过证据")
            records = (*state.verification_records, record)
            current = [r for r in records if r.revision == state.work_revision]
            # 同一目标的最新检查生效；保留完整历史。
            latest = {r.goal: r for r in current}
            passed = all(r.result == "passed" for r in latest.values())
            status = (
                "passed"
                if passed
                else "failed"
                if any(r.result == "failed" for r in latest.values())
                else "unchecked"
            )
            return (
                {"verification_records": records, "verification": status, "conclusion": None},
                "验证记录已保存。",
                True,
            )
        conclusion = TaskConclusion.model_validate(dict(args))
        if not conclusion.summary.strip():
            raise ValueError("summary 不得为空")
        if conclusion.status == "completed":
            if conclusion.remaining or any(p.status != "completed" for p in state.plan):
                raise ValueError("仍有未完成计划或 remaining 项，不能声明全部完成")
            if state.verification in ("unchecked", "failed"):
                raise ValueError("改动尚未通过目标验证，不能声明全部完成")
        return {"conclusion": conclusion}, "任务结论已记录，请给出与此一致的最终回复。", True
    except (ValidationError, ValueError) as exc:
        return {}, f"未记录：{exc}", False
