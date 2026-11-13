"""Persistent child follow-ups and partial evidence survive finalisation/interruption."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.core import AgentTool, Message, ToolResult
from agent_runtime.llm import Degraded, LLMRequest, TokenUsage
from agent_runtime.subagents.delegate.store import ChildConversation, ChildStore
from agent_runtime.subagents.usage import ChildUsage
from tests.helpers.gateway_support import call_response, parsed
from tests.subagents.test_delegate import (
    ARGS,
    Gateway,
    RawSubagentReport,
    call,
    finding,
    runner,
    spawn_tool,
    tools,
)
from tests.tools.test_tools_core import Fake
from tools.core import ToolRegistry


@pytest.mark.integration
async def test_followup_uses_saved_context_after_reconstructing_tool(tmp_path: Path) -> None:
    first = spawn_tool(storage_dir=tmp_path)
    result = await first.invoke(ARGS)
    child_id = result.meta["subtask_id"]
    assert result.artifact_id == child_id
    assert result.artifact_path == str(tmp_path / f"{child_id}.evidence.txt")

    followup = spawn_tool(storage_dir=tmp_path)
    continued = await followup.invoke({"subtask_id": child_id, "goal": "再解释端口归属"})
    assert continued.ok and continued.meta["subtask_id"] == child_id
    saved = ChildStore(tmp_path).load(child_id)
    assert [m.content for m in saved.messages if m.role == "user"] == [
        "读代码",
        "再解释端口归属",
    ]
    assert len([m for m in saved.messages if m.role == "system"]) == 1
    assert saved.assigned_tools == ["view_file", "grep"]
    assert saved.report is not None


@pytest.mark.security
async def test_followup_cannot_replace_authorization_or_escape_parent(tmp_path: Path) -> None:
    tool = spawn_tool(storage_dir=tmp_path / "parent")
    original = await tool.invoke(ARGS)
    followup = {"subtask_id": original.meta["subtask_id"], "goal": "继续"}
    assert (await tool.invoke({**followup, "assigned_tools": ["write_file"]})).failed
    assert (await tool.invoke({**followup, "role": "writer"})).failed
    assert (await spawn_tool(storage_dir=tmp_path / "other").invoke(followup)).failed
    assert (await tool.invoke({**followup, "subtask_id": "../elsewhere"})).failed
    with ChildStore(tmp_path / "parent").lease(followup["subtask_id"]):
        assert "正在运行" in (await tool.invoke(followup)).content


@pytest.mark.integration
async def test_failed_extraction_returns_observations_and_preserves_full_output(
    tmp_path: Path,
) -> None:
    full_output = "关键反证：服务未监听\n" + "详细现场\n" * 1000
    view = Fake("view_file", idempotent=True, result=ToolResult(ok=True, content=full_output))
    degraded = Degraded(
        "invalid", "x", ("p",), parsed(RawSubagentReport()).response, TokenUsage(1, 1, 2), 2
    )
    gateway = Gateway(
        [call("c1", "view_file", path="src/state.txt"), call_response("尚未解决")], degraded
    )
    child = ChildConversation.create("reader", ["view_file"])
    store = ChildStore(tmp_path)
    report = await runner(gateway, ToolRegistry([view], allow_untrusted=True)).run(
        "reader",
        "调查",
        ChildUsage(10),
        4,
        conversation=child,
        store=store,
    )
    assert report.status == "partial"
    assert any("服务未监听" in finding.statement for finding in report.findings)
    raw = json.loads(store.path(child.subtask_id).read_text())
    assert raw["observations"][0]["result"]["content"] == full_output
    assert raw["access"]["files"] == ["src/state.txt"]
    evidence = store.evidence_path(child.subtask_id).read_text()
    assert full_output in evidence
    assert "你是 reader" not in evidence


@pytest.mark.integration
async def test_timeout_preserves_completed_call_and_does_not_replay_it(tmp_path: Path) -> None:
    started = asyncio.Event()

    class Slow(Fake):
        async def invoke(self, args: Any) -> ToolResult:
            started.set()
            await asyncio.sleep(30)
            return ToolResult(ok=True)

    fast = Fake("view_file", idempotent=True, result=ToolResult(ok=True, content="已确认进程退出"))
    slow = Slow("grep", idempotent=True)
    registry = ToolRegistry([fast, slow], allow_untrusted=True)
    gateway = Gateway(
        [call_response("", [("fast", "view_file", {"path": "a"}), ("slow", "grep", {})])]
    )
    child = ChildConversation.create("reader", ["view_file", "grep"])
    store = ChildStore(tmp_path)
    report = await runner(gateway, registry).run(
        "reader",
        "调查",
        ChildUsage(0.1),
        4,
        conversation=child,
        store=store,
    )
    assert started.is_set() and report.status == "partial"
    saved = store.load(child.subtask_id)
    assert len(saved.observations) == 1
    assert any("已确认进程退出" in f.statement for f in report.findings)
    assert any("结果未知" in m.content for m in saved.messages)
    resumed_gateway = Gateway(
        [call_response("继续核查")], RawSubagentReport(findings=[finding("解释")])
    )
    await runner(resumed_gateway, registry).run(
        "reader",
        "继续",
        ChildUsage(10),
        4,
        conversation=saved,
        store=store,
    )
    assert len(fast.calls) == 1
    assert any("已确认进程退出" in m.content for m in resumed_gateway.requests[0].messages)


@pytest.mark.integration
async def test_cancellation_saves_completed_observations_before_parent_returns(
    tmp_path: Path,
) -> None:
    waiting = asyncio.Event()

    class Hang(Gateway):
        async def invoke(self, request: LLMRequest) -> Any:
            if any(m.role == "tool" for m in request.messages):
                waiting.set()
                await asyncio.sleep(30)
            return call("read", "view_file", path="a")

    child = ChildConversation.create("reader", ["view_file"])
    store = ChildStore(tmp_path)
    task = asyncio.create_task(
        runner(Hang([]), tools()).run(
            "reader",
            "调查",
            ChildUsage(60),
            4,
            conversation=child,
            store=store,
        )
    )
    await asyncio.wait_for(waiting.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = store.load(child.subtask_id)
    assert saved.status == "interrupted" and len(saved.observations) == 1
    assert saved.messages[-1].role == "tool"


@pytest.mark.integration
async def test_interrupted_pending_calls_are_not_automatically_reexecuted(tmp_path: Path) -> None:
    child = ChildConversation.create("reader", ["write_file"])
    child.messages = [
        Message(role="system", content="system"),
        Message(role="user", content="original"),
        call("pending", "write_file", path="a", content="x").message,
    ]
    writer = Fake("write_file")
    gateway = Gateway([call_response("先核查")])
    await runner(gateway, ToolRegistry([writer], allow_untrusted=True)).run(
        "reader",
        "继续",
        ChildUsage(10),
        4,
        conversation=child,
        store=ChildStore(tmp_path),
    )
    assert writer.calls == []
    assert any("结果未知" in m.content for m in gateway.requests[0].messages)


@pytest.mark.integration
async def test_followup_retains_previous_access_evidence_without_rereading(tmp_path: Path) -> None:
    store = ChildStore(tmp_path)
    child = ChildConversation.create("reader", ["view_file"])
    registry = ToolRegistry([Fake("view_file", idempotent=True)], allow_untrusted=True)
    first = Gateway([call("read", "view_file", path="src/app.py"), call_response("读完")])
    await runner(first, registry).run(
        "reader",
        "调查",
        ChildUsage(10),
        4,
        conversation=child,
        store=store,
    )
    second = Gateway(
        [call_response("进一步说明")],
        RawSubagentReport(findings=[finding("已有证据", ("file", "src/app.py"))]),
    )
    report = await runner(second, registry).run(
        "reader",
        "继续",
        ChildUsage(10),
        4,
        conversation=store.load(child.subtask_id),
        store=store,
    )
    assert report.status == "success" and report.findings[0].verified
    assert report.tool_calls == 0


@pytest.mark.security
async def test_parent_outcome_tools_cannot_be_assigned_to_children(tmp_path: Path) -> None:
    parent: dict[str, AgentTool] = {
        name: Fake(name) for name in ("view_file", "report_outcome", "record_verification")
    }
    tool = spawn_tool(parent=parent, storage_dir=tmp_path)
    for name in ("report_outcome", "record_verification"):
        result = await tool.invoke({**ARGS, "assigned_tools": ["view_file", name]})
        assert result.failed and "不能分配" in result.content
