"""Read the original task transcript after context folding or summarisation."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from agent_runtime.core import AgentTool, Message, ToolResult


class TaskRecordView:
    def __init__(self) -> None:
        self.messages: tuple[Message, ...] = ()

    def bind(self, messages: Sequence[Message]) -> None:
        self.messages = tuple(messages)


class ReadTaskRecordTool(AgentTool):
    name = "read_task_record"
    description = (
        "回读本任务未压缩的原始消息记录，核对摘要或寻找反证。start 为零起始消息下标，"
        "limit 最多20条；query 为原文子串，可配 call_id 精确定位调用和结果。"
        "单条正文可用 offset/max_chars 翻页。被工具输出层裁剪的全文用 read_artifact 读取。"
    )
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "start": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "query": {"type": "string"},
            "call_id": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "max_chars": {"type": "integer", "minimum": 1, "maximum": 8000},
        },
        "additionalProperties": False,
    }
    idempotent = True
    trust = "untrusted"

    def __init__(self, view: TaskRecordView) -> None:
        self._view = view

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult:
        values: dict[str, int] = {}
        for key, default, lower, upper in (
            ("start", 0, 0, 2**63 - 1),
            ("limit", 10, 1, 20),
            ("offset", 0, 0, 2**63 - 1),
            ("max_chars", 2000, 1, 8000),
        ):
            value = args.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                return ToolResult.failure(f"{key} 必须是 {lower}..{upper} 的整数")
            values[key] = value
        query, call_id = args.get("query", ""), args.get("call_id", "")
        if not isinstance(query, str) or not isinstance(call_id, str):
            return ToolResult.failure("query 和 call_id 必须是字符串")
        records: list[dict[str, object]] = []
        next_start: int | None = None
        for index in range(values["start"], len(self._view.messages)):
            message = self._view.messages[index]
            calls = [dict(id=c.id, name=c.name, args=dict(c.args)) for c in message.tool_calls]
            if (
                call_id
                and message.tool_call_id != call_id
                and not any(c["id"] == call_id for c in calls)
            ):
                continue
            original = message.content
            if calls:
                original += "\n" + json.dumps(calls, ensure_ascii=False)
            if query.lower() not in original.lower():
                continue
            if len(records) == values["limit"]:
                next_start = index
                break
            offset, cap = values["offset"], values["max_chars"]
            end = min(offset + cap, len(original))
            records.append(
                {
                    "index": index,
                    "role": message.role,
                    "name": message.name,
                    "call_id": message.tool_call_id,
                    "text": original[offset:end],
                    "total_chars": len(original),
                    "offset": offset,
                    "next_offset": end if end < len(original) else None,
                }
            )
        return ToolResult(
            ok=True,
            content=json.dumps({"records": records, "next_start": next_start}, ensure_ascii=False),
            trust="untrusted",
            is_truncated=next_start is not None
            or any(r["next_offset"] is not None for r in records),
        )
