"""目标结果、显式验证与逐调用恢复的回归用例。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from agent_runtime.checkpoint.tool_journal import ToolJournal
from agent_runtime.core import PlanItem, apply_update
from agent_runtime.core.codec import dumps_state, loads_state
from agent_runtime.core.state import TaskConclusion, ToolObservation
from agent_runtime.loop.conclusion import assess
from agent_runtime.loop.kernel import TaskOutcome
from agent_runtime.loop.tools import make_tool_runner
from tests.runtime.test_loop_tools import Fake, call, make_runner, state_with


def test_end_is_not_completion() -> None:
    state = state_with([])
    assert TaskOutcome(state).status == "partial"
    for status in ("completed", "partial", "needs_input", "blocked"):
        updated = apply_update(state, {"conclusion": TaskConclusion(status=status, summary="依据")})
        assert TaskOutcome(updated).status == ("succeeded" if status == "completed" else status)
        assert loads_state(dumps_state(updated)) == updated


def test_completed_rejects_open_plan_or_unverified_changes() -> None:
    args = {"status": "completed", "summary": "完成", "remaining": []}
    for state in (
        state_with([], plan=(PlanItem(id=1, title="修复"),)),
        state_with([], verification="unchecked"),
        state_with([], verification="failed"),
    ):
        assert not assess(state, "report_outcome", args)[2]
    assert assess(state_with([]), "report_outcome", args)[2]


@pytest.mark.asyncio
async def test_pwd_does_not_verify_file_changes(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, [Fake("write_file"), Fake("bash")])
    calls = [call("edit", "write_file", path="x"), call("pwd", "bash", command="pwd")]
    state = apply_update(state_with(calls), await runner(state_with(calls), tuple(calls)))
    assert state.verification == "unchecked"
    assert not state.verification_records
    assert state.observations[-1].call_id == "pwd"


def test_verification_requires_current_executed_evidence() -> None:
    state = state_with(
        [],
        work_revision=2,
        verification="unchecked",
        observations=(
            ToolObservation(call_id="old", tool="bash", ok=True, revision=1),
            ToolObservation(call_id="bad", tool="bash", ok=False, revision=2),
            ToolObservation(call_id="edit", tool="write_file", ok=True, revision=2),
            ToolObservation(call_id="check", tool="bash", ok=True, revision=2),
        ),
    )
    args = {"goal": "服务恢复", "check": "HTTP 健康检查", "result": "passed", "summary": "200 OK"}
    for ident in ("missing", "old", "bad", "edit"):
        assert not assess(state, "record_verification", {**args, "evidence_call_ids": [ident]})[2]
    update, _, ok = assess(state, "record_verification", {**args, "evidence_call_ids": ["check"]})
    assert ok
    verified = apply_update(state, update)
    assert verified.verification == "passed"
    assert verified.verification_records[0].goal == "服务恢复"
    assert loads_state(dumps_state(verified)) == verified


@pytest.mark.asyncio
async def test_each_result_survives_other_call_cancellation(tmp_path: Path) -> None:
    from agent_runtime.loop.tools import ToolRunnerDeps

    # Reuse the established fixture dependencies without changing production behavior.
    original = make_runner(tmp_path, [Fake("slow_a"), Fake("slow_b", delay=20)])
    deps = next(
        cell.cell_contents
        for cell in original.__closure__ or ()
        if isinstance(cell.cell_contents, ToolRunnerDeps)
    )
    journal = ToolJournal(tmp_path / "calls.sqlite3")
    runner = make_tool_runner(replace(deps, journal=journal))
    calls = (call("a", "slow_a"), call("b", "slow_b"))
    state = state_with(list(calls))
    task = asyncio.create_task(runner(state, calls))
    for _ in range(100):
        await asyncio.sleep(0.005)
        if journal.load("t", 1, "a")[0] == "completed":
            break
    assert journal.load("t", 1, "a")[0] == "completed"
    assert journal.load("t", 1, "b")[0] == "running"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    restored = apply_update(state, await runner(state, calls))
    replies = {m.tool_call_id: m.content for m in restored.messages if m.role == "tool"}
    assert replies["a"] == "slow_a done"
    assert "执行结果未知" in replies["b"]
    assert restored.observations[0].ok
    assert not restored.observations[1].ok
    journal.delete("t")
    assert journal.entries("t") == []


@pytest.mark.asyncio
async def test_outcome_tools_use_batch_checks_in_order(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, [Fake("bash")])
    calls = [
        call("check", "bash", command="pytest"),
        call(
            "verify",
            "record_verification",
            goal="目标",
            check="单元验证",
            result="passed",
            summary="通过",
            evidence_call_ids=["check"],
        ),
        call("outcome", "report_outcome", status="completed", summary="目标已验证", remaining=[]),
    ]
    state = state_with(calls, verification="unchecked")
    final = apply_update(state, await runner(state, tuple(calls)))
    assert final.verification == "passed"
    assert final.conclusion is not None and final.conclusion.status == "completed"
    assert TaskOutcome(final).status == "succeeded"


@pytest.mark.asyncio
async def test_outcome_before_new_pending_plan_cannot_leave_completed(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, [])
    calls = [
        call("done", "report_outcome", status="completed", summary="已完成", remaining=[]),
        call("plan", "update_plan", items=[{"id": 1, "title": "还需检查", "status": "pending"}]),
    ]
    state = state_with(calls)
    final = apply_update(state, await runner(state, tuple(calls)))
    assert final.plan and final.plan[0].status == "pending"
    assert TaskOutcome(final).status != "succeeded"


@pytest.mark.asyncio
async def test_shell_mutation_invalidates_previous_verification(tmp_path: Path) -> None:
    from agent_runtime.loop.tools import ToolRunnerDeps

    original = make_runner(tmp_path, [Fake("bash")])
    deps = next(
        cell.cell_contents
        for cell in original.__closure__ or ()
        if isinstance(cell.cell_contents, ToolRunnerDeps)
    )
    policy = replace(deps.policy, workspace_write_patterns=(r"\bsed\b",))
    runner = make_tool_runner(replace(deps, policy=policy))
    calls = [call("change", "bash", command="sed -i s/old/new/ config.txt")]
    state = state_with(calls, verification="passed", work_revision=2)
    final = apply_update(state, await runner(state, tuple(calls)))
    assert final.work_revision > 2
    assert final.verification == "unchecked"
    assert not assess(
        final, "report_outcome", {"status": "completed", "summary": "done", "remaining": []}
    )[2]


@pytest.mark.asyncio
async def test_prepare_resume_reuses_completed_and_executes_only_queued(tmp_path: Path) -> None:
    from agent_runtime.checkpoint import CheckpointStore
    from agent_runtime.core import ToolResult
    from agent_runtime.loop.tools import ToolRunnerDeps
    from agent_runtime.runtime.resume import prepare_resume
    from tests.runtime.test_resume import Harness

    log: list[str] = []
    original = make_runner(tmp_path, [Fake("write_file", log=log)])
    deps = next(
        cell.cell_contents
        for cell in original.__closure__ or ()
        if isinstance(cell.cell_contents, ToolRunnerDeps)
    )
    calls = (call("done", "write_file", path="a"), call("queued", "write_file", path="b"))
    state = state_with(list(calls))
    async with CheckpointStore(tmp_path / "checkpoints.sqlite3") as store:
        await store.commit(state)
        journal = ToolJournal(store.journal_path)
        journal.enqueue("t", 1, [c.id for c in calls])
        journal.start("t", 1, "done")
        journal.finish("t", 1, "done", ToolResult(ok=True, content="saved result"), "trusted")
        prepared = await prepare_resume(Harness(store).deps, "t")
        assert prepared == state  # no synthetic unknown results for already journaled calls
        runner = make_tool_runner(replace(deps, journal=journal))
        restored = apply_update(prepared, await runner(prepared, calls))
        assert log == ["start:write_file", "end:write_file"]
        replies = {m.tool_call_id: m.content for m in restored.messages if m.role == "tool"}
        assert replies == {"done": "saved result", "queued": "write_file done"}
        assert journal.load("t", 1, "queued")[0] == "completed"


@pytest.mark.asyncio
async def test_runner_cancel_preserves_completed_running_and_queued_truth(tmp_path: Path) -> None:
    from agent_runtime.checkpoint import CheckpointStore, RecoveryReport
    from agent_runtime.core import ToolResult
    from tests.runtime.test_task_runner import World

    calls = [
        call("done", "write_file"),
        call("running", "write_file"),
        call("queued", "write_file"),
    ]
    state = state_with(calls)
    async with CheckpointStore(tmp_path / "checkpoints.sqlite3") as store:
        await store.commit(state)
        journal = ToolJournal(store.journal_path)
        journal.enqueue("t", 1, [c.id for c in calls])
        journal.start("t", 1, "done")
        journal.finish(
            "t",
            1,
            "done",
            ToolResult(
                ok=True, content="real output", artifact_id="saved", artifact_path="/tmp/saved.txt"
            ),
            "trusted",
        )
        journal.start("t", 1, "running")
        runner = World(store).runner()
        # Adopt as waiting so cancellation uses the real persisted task without starting a model.
        await runner.adopt(RecoveryReport(interrupted=(), waiting_for_approval=("t",)))
        assert await runner.cancel("t")
        final = await store.load("t")
        assert final is not None
        replies = {m.tool_call_id: m.content for m in final.messages if m.role == "tool"}
        assert replies["done"] == "real output"
        assert "执行结果未知" in replies["running"]
        assert "本调用没有执行" in replies["queued"]
        assert final.artifacts["saved"] == "/tmp/saved.txt"
        assert runner.snapshot("t").status == "cancelled"


@pytest.mark.asyncio
async def test_read_only_command_keeps_existing_verification(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, [Fake("bash")])
    calls = [call("pwd", "bash", command="pwd")]
    state = state_with(calls, verification="passed", work_revision=2)
    final = apply_update(state, await runner(state, tuple(calls)))
    assert final.work_revision == 2 and final.verification == "passed"


@pytest.mark.asyncio
@pytest.mark.parametrize("ok", [True, False])
async def test_service_mutation_even_failed_requires_fresh_verification(
    tmp_path: Path, ok: bool
) -> None:
    from agent_runtime.loop.tools import ToolRunnerDeps

    original = make_runner(tmp_path, [Fake("bash", ok=ok)])
    deps = next(
        cell.cell_contents
        for cell in original.__closure__ or ()
        if isinstance(cell.cell_contents, ToolRunnerDeps)
    )
    policy = replace(deps.policy, workspace_write_patterns=(r"\bsystemctl\b",))
    runner = make_tool_runner(replace(deps, policy=policy))
    calls = [call("restart", "bash", command="systemctl restart app")]
    state = state_with(calls, verification="passed", work_revision=2)
    final = apply_update(state, await runner(state, tuple(calls)))
    assert final.work_revision == 3 and final.verification == "unchecked"


@pytest.mark.asyncio
async def test_invalid_outcome_cannot_reuse_prior_completion(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, [])
    calls = [call("bad", "report_outcome", status="completed", summary="", remaining=[])]
    state = state_with(calls, conclusion=TaskConclusion(status="completed", summary="旧结论"))
    final = apply_update(state, await runner(state, tuple(calls)))
    assert TaskOutcome(final).status == "partial"


@pytest.mark.asyncio
async def test_unknown_write_invalidates_old_verification(tmp_path: Path) -> None:
    from agent_runtime.loop.tools import ToolRunnerDeps

    original = make_runner(tmp_path, [Fake("write_file")])
    deps = next(
        cell.cell_contents
        for cell in original.__closure__ or ()
        if isinstance(cell.cell_contents, ToolRunnerDeps)
    )
    journal = ToolJournal(tmp_path / "calls.sqlite3")
    journal.start("t", 1, "write")
    runner = make_tool_runner(replace(deps, journal=journal))
    calls = [call("write", "write_file", path="changed.txt")]
    state = state_with(calls, verification="passed", work_revision=1)
    final = apply_update(state, await runner(state, tuple(calls)))
    assert final.verification == "unchecked"
    assert final.work_revision == 2
