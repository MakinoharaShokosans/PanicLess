from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime.memory import MemoryStore
from agent_runtime.memory.experience import HistoricalCase, rank_cases
from tests.helpers.gateway_support import MakeState
from tests.memory.test_memory_flow import Ex, finished, flow


def test_relevance_beats_recency_and_unrelated_cases_are_absent() -> None:
    old = HistoricalCase("old", "s1", "nginx 端口冲突", "success", "监听端口被占用", 1)
    new = HistoricalCase("new", "s2", "redis 内存", "success", "增加内存", 999)
    assert rank_cases("nginx 端口冲突如何检查", [new, old], 5) == [old]
    assert rank_cases("postgres database", [old, new], 5) == []
    assert rank_cases("", [old, new], 5) == []


@pytest.mark.unit
async def test_cases_survive_reopen_and_cross_sessions(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    async with MemoryStore(path) as store:
        await store.create_workspace("w1", "test", str(tmp_path))
        await store.create_session("s1", "w1")
        await store.save_case(
            HistoricalCase("t1", "s1", "nginx timeout", "success", "check upstream", 1)
        )
    async with MemoryStore(path) as store:
        await store.create_session("s2", "w1")
        ctx = await flow(store).prepare("s2", "nginx timeout")
        assert len(ctx.assets.related_cases) == 1
        text = ctx.assets.related_cases[0].text
        assert "task_id=t1" in text and "1970-01-01" in text
        assert "不能据此认定当前机器状态" in text
        assert ctx.assets.related_cases[0].trust == "untrusted"
        assert not (await flow(store).prepare("s2", "redis memory")).assets.related_cases
        await store.delete_session("s1")
        assert await store.search_cases("nginx") == []


@pytest.mark.unit
async def test_finish_separates_experience_from_timestamped_observation(
    tmp_path: Path, make_state: MakeState
) -> None:
    async with MemoryStore(tmp_path / "memory.db") as store:
        await store.create_workspace("w1", "test", str(tmp_path))
        await store.create_session("s1", "w1")
        f = flow(store, Ex(), max_pending=20)
        state = finished(
            make_state,
            facts=("[experience] nginx 端口冲突先查监听者", "nginx 当前监听8080"),
        )
        report = await f.finish("s1", state, delivery="done")
        assert report.error is None
        import asyncio

        await asyncio.gather(*f._background_tasks)
        memory = await store.load_memory("s1")
        facts = (*memory.facts, *memory.pending_facts)
        assert {fact.kind for fact in facts} == {"experience", "observation"}
        matches = await store.search_cases("nginx")
        assert len(matches) == 1
        assert "稳定经验" in matches[0].content
        assert "历史观察（非当前状态）" in matches[0].content
        assert "1970-01-01T00:01:40+00:00" in matches[0].content
        import dataclasses

        await store.update_memory(
            "s1", lambda m: dataclasses.replace(m, facts=(), pending_facts=())
        )
        assert len(await store.search_cases("nginx")) == 1  # 会话记忆淘汰不影响案例归档


def test_compressed_observation_does_not_claim_fresh_collection() -> None:
    from agent_runtime.memory.experience import fact_text
    from agent_runtime.memory.models import Fact

    text = fact_text(Fact("磁盘满", "old-task", "trusted", "compressor", 1000))
    assert "提炼时间=" in text
    assert "原始采集时间未知" in text
    assert "task_id=old-task" in text
    assert "采集时间=" not in text
