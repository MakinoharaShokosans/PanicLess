"""任务补充消息的持久化、边界消费与最终回复竞争。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from agent_runtime.checkpoint import CheckpointStore
from agent_runtime.core import AgentState, Message
from agent_runtime.runtime.errors import TaskAlreadyRunningError
from tests.runtime.test_task_runner import World, request


@pytest.mark.unit
async def test_message_arriving_during_final_reply_runs_another_turn(tmp_path: Path) -> None:
    async with CheckpointStore(tmp_path / "state.db") as store:
        world = World(store)
        entered, release = asyncio.Event(), asyncio.Event()
        seen: list[AgentState] = []

        async def model(state: AgentState) -> Message:
            seen.append(state)
            entered.set()
            await release.wait()
            return Message(role="assistant", content="done")

        async def build(state: AgentState):
            return replace(await world.build(state), model=model)

        from agent_runtime.runtime import TaskRunner

        runner = TaskRunner(replace(world.deps(), build_runtime=build))
        task_id = await runner.submit(request())
        await entered.wait()
        item = await runner.send_message(task_id, "更正：端口是 8081")
        assert item["message_id"]
        release.set()
        snapshot = await runner.wait(task_id)
        assert len(seen) == 2
        assert seen[-1].task_goal == "fix the bug"
        assert seen[-1].messages[-1].content == "更正：端口是 8081"
        assert snapshot.state is not None
        assert snapshot.state.inbox_cursor == item["message_id"]
        assert sum(m.content == "更正：端口是 8081" for m in snapshot.state.messages) == 1
        with pytest.raises(TaskAlreadyRunningError):
            await runner.send_message(task_id, "too late")


@pytest.mark.unit
async def test_paused_task_queues_until_resume_and_persists_cursor(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    async with CheckpointStore(path) as store:
        world = World(store)
        world.gate = asyncio.Event()
        runner = world.runner()
        task_id = await runner.submit(request())
        await world.entered.wait()
        runner.control(task_id, "pause")
        world.gate.set()
        await asyncio.wait_for(runner._records[task_id].paused.wait(), 2)
        item = await runner.send_message(task_id, "不要重启，先检查配置")
        state = await store.load(task_id)
        assert state is not None and state.inbox_cursor == 0
        runner.control(task_id, "resume")
        final = await runner.wait(task_id)
        assert final.state is not None and final.state.inbox_cursor == item["message_id"]
    async with CheckpointStore(path) as reopened:
        restored = await reopened.load(task_id)
        assert restored is not None
        assert await reopened.inbox(task_id, after=restored.inbox_cursor) == []
        assert len(await reopened.inbox(task_id)) == 1
        await reopened.delete(task_id)
        assert await reopened.inbox(task_id) == []


@pytest.mark.unit
async def test_pending_inbox_survives_shutdown_and_resume(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    async with CheckpointStore(path) as store:
        world = World(store)
        world.gate = asyncio.Event()
        runner = world.runner()
        task_id = await runner.submit(request())
        await world.entered.wait()
        await runner.send_message(task_id, "修正故障目标")
        await runner.shutdown()
    async with CheckpointStore(path) as store:
        world = World(store)
        runner = world.runner()
        await runner.resume(task_id)
        final = await runner.wait(task_id)
        assert final.state is not None
        assert sum(m.content == "修正故障目标" for m in final.state.messages) == 1
        assert await store.inbox(task_id, after=final.state.inbox_cursor) == []


@pytest.mark.unit
async def test_message_consumed_before_model_failure_is_not_replayed(tmp_path: Path) -> None:
    async with CheckpointStore(tmp_path / "state.db") as store:
        world = World(store)
        runner = world.runner()
        world.gate = asyncio.Event()
        task_id = await runner.submit(request())
        await world.entered.wait()
        runner.control(task_id, "pause")
        world.gate.set()
        await asyncio.wait_for(runner._records[task_id].paused.wait(), 2)
        await runner.send_message(task_id, "保留反证")
        world.fail_model = RuntimeError("model offline")
        runner.control(task_id, "resume")
        assert (await runner.wait(task_id)).status == "failed"
        committed = await store.load(task_id)
        assert committed is not None and committed.inbox_cursor > 0
        world.fail_model = None
        await runner.resume(task_id)
        result = await runner.wait(task_id)
        assert result.state is not None
        assert sum(m.content == "保留反证" for m in result.state.messages) == 1


@pytest.mark.unit
async def test_waiting_approval_accepts_message_without_bypassing_approval(tmp_path: Path) -> None:
    async with CheckpointStore(tmp_path / "state.db") as store:
        world = World(store)
        world.needs_approval = True
        runner = world.runner()
        task_id = await runner.submit(request())
        assert (await runner.wait(task_id)).status == "waiting_for_approval"
        await runner.send_message(task_id, "补充端口信息")
        assert runner.snapshot(task_id).status == "waiting_for_approval"
        waiting = await store.load(task_id)
        assert waiting is not None and waiting.pending_approval is not None
        assert waiting.inbox_cursor == 0
        await runner.resume(
            task_id,
            {
                "approval_id": waiting.pending_approval.approval_id,
                "approved": True,
            },
        )
        final = await runner.wait(task_id)
        assert final.state is not None
        assert any(m.content == "补充端口信息" for m in final.state.messages)
