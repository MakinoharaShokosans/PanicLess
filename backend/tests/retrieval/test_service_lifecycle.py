"""检索资源关闭和构造取消；使用独立替身，不打开正式库或模型连接。"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from baseagent_config.models import RetrievalConfig
from baseagent_config.retrieval import cold_values
from tools.retrieval.records import Corpus, Generation
from tools.retrieval.service import RetrievalService

pytestmark = pytest.mark.unit


class FakeStore:
    def __init__(self, config, shared_with=None):
        self.config = config
        self.closed = 0
        self.cleanup_ids = set()

    def active(self, workspace):
        generation = Generation(
            "generation",
            workspace,
            "fingerprint",
            3,
            {"cold": cold_values(self.config)},
            0,
        )
        return Corpus(generation, (), ())

    def collect(self, pinned):
        pass

    def close(self):
        self.closed += 1


class FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.closed = 0

    async def aclose(self):
        self.closed += 1
        if self.error:
            raise self.error


@pytest.mark.parametrize("failing_client", ["embedding", "rerank"])
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_snapshot_close_failure_releases_users_and_pins(
    tmp_path: Path, failing_client: str, error_type: type[BaseException]
) -> None:
    config = RetrievalConfig()
    clients = {
        name: FakeClient(error_type("close failed") if name == failing_client else None)
        for name in ("embedding", "rerank")
    }
    service = RetrievalService(
        config,
        control_path=tmp_path / "control.json",
        embedding_factory=lambda _: clients["embedding"],
        rerank_factory=lambda _: clients["rerank"],
    )
    store = FakeStore(config)
    service._store = store

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    service.run = run_inline
    try:
        with pytest.raises(error_type, match="close failed"):
            async with service.snapshot():
                assert service._users[store] == 1
                assert service._pins["generation"] == 1
                assert not service._idle.is_set()
        assert clients["embedding"].closed == clients["rerank"].closed == 1
        assert service._users[store] == 0
        assert service._pins["generation"] == 0
        assert service._idle.is_set()
    finally:
        await asyncio.wait_for(service.aclose(), 1)
    assert store.closed == 1


@pytest.mark.parametrize("shared", [False, True])
async def test_cancelled_store_constructor_closes_its_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shared: bool
) -> None:
    config = RetrievalConfig()
    service = RetrievalService(config, control_path=tmp_path / "control.json")
    started, finish = threading.Event(), threading.Event()
    created = []
    parent = FakeStore(config) if shared else None

    def blocked_constructor(config, shared_with=None):
        assert shared_with is parent
        started.set()
        assert finish.wait(2), "test did not release constructor"
        store = FakeStore(config, shared_with)
        created.append(store)
        return store

    monkeypatch.setattr("tools.retrieval.service.KnowledgeStore", blocked_constructor)
    task = asyncio.create_task(service._open_store(config, parent))
    try:
        async with asyncio.timeout(2):
            while not started.is_set():  # noqa: ASYNC110 - observe a worker-thread barrier
                await asyncio.sleep(0.005)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            finish.set()
            # A short timer also keeps executor wakeups observable in sandboxed runners.
            while not task.done():  # noqa: ASYNC110 - sandbox executor wakeup timer
                await asyncio.sleep(0.005)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(created) == 1 and created[0].closed == 1
        assert parent is None or parent.closed == 0
    finally:
        finish.set()
        await service.aclose()


async def test_cancelled_open_releases_control_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    config = RetrievalConfig()
    control = tmp_path / "control.json"
    service = RetrievalService(config, control_path=control)
    started, finish = threading.Event(), threading.Event()
    created = []

    def blocked_constructor(config, shared_with=None):
        started.set()
        assert finish.wait(2)
        store = FakeStore(config, shared_with)
        created.append(store)
        return store

    monkeypatch.setattr("tools.retrieval.service.KnowledgeStore", blocked_constructor)
    task = asyncio.create_task(service.open())
    try:
        async with asyncio.timeout(2):
            while not started.is_set():  # noqa: ASYNC110 - observe a worker-thread barrier
                await asyncio.sleep(0.005)
            task.cancel()
            finish.set()
            while not task.done():  # noqa: ASYNC110 - sandbox executor wakeup timer
                await asyncio.sleep(0.005)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert created[0].closed == 1
        assert service._store is None and service._control_lock is None
        with control.with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        finish.set()
        await service.aclose()
