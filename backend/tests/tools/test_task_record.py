from __future__ import annotations

import json

import pytest

from agent_runtime.core import Message, ToolCall
from tools.builtin.task_record import ReadTaskRecordTool, TaskRecordView


@pytest.mark.unit
async def test_read_original_counterevidence_after_truncation() -> None:
    view = TaskRecordView()
    original = "healthy\n" + "x" * 5000 + "\nCOUNTEREVIDENCE service exited"
    view.bind(
        (
            Message(
                role="assistant",
                content="",
                tool_calls=(ToolCall(id="c1", name="bash", args={"command": "probe"}),),
            ),
            Message(role="tool", tool_call_id="c1", name="bash", content=original),
        )
    )
    tool = ReadTaskRecordTool(view)
    result = await tool.invoke({"call_id": "c1", "query": "COUNTEREVIDENCE", "offset": 5000})
    payload = json.loads(result.content)
    assert result.ok and result.trust == "untrusted"
    assert payload["records"][0]["index"] == 1
    assert "service exited" in payload["records"][0]["text"]
    assert payload["records"][0]["next_offset"] is None


@pytest.mark.unit
async def test_record_pagination_and_rebinding() -> None:
    view = TaskRecordView()
    view.bind(tuple(Message(role="user", content=str(i)) for i in range(3)))
    tool = ReadTaskRecordTool(view)
    first = await tool.invoke({"limit": 1})
    payload = json.loads(first.content)
    assert first.is_truncated and payload["next_start"] == 1
    second = await tool.invoke({"start": payload["next_start"], "limit": 2})
    assert [r["text"] for r in json.loads(second.content)["records"]] == ["1", "2"]
    assert not second.is_truncated
    view.bind(())
    assert json.loads((await tool.invoke({})).content)["records"] == []
    assert not (await tool.invoke({"limit": True})).ok
