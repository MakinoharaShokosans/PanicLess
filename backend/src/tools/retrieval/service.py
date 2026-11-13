"""知识库应用服务：配置快照、串行管理、索引代发布和资源生命周期。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, Self

from baseagent_config.models import EmbeddingConfig, RerankConfig, RetrievalConfig
from baseagent_config.retrieval import (
    cold_values,
    model_fingerprint,
    pending_fields,
    revision,
    with_active_identity,
)
from tools.retrieval.chunking import scan_source
from tools.retrieval.client import EmbeddingClient, RerankClient
from tools.retrieval.errors import RetrievalServiceError
from tools.retrieval.indexing import build, prepare
from tools.retrieval.records import KNOWLEDGE_INDEX, Corpus
from tools.retrieval.storage import KnowledgeStore

_STORAGE = ("persist_dir", "sqlite_path", "chroma_path")


@dataclass
class RequestSnapshot:
    config: RetrievalConfig
    config_revision: str
    store: KnowledgeStore
    corpus: Corpus | None
    embedding: Any
    rerank: Any


class RetrievalService:
    """默认工厂为正式 HTTP 客户端；模型模拟只允许调用方显式注入。"""

    def __init__(
        self,
        config: RetrievalConfig,
        *,
        control_path: Path,
        reload_config: Callable[[], RetrievalConfig] | None = None,
        embedding_factory: Callable[[EmbeddingConfig], Any] = EmbeddingClient,
        rerank_factory: Callable[[RerankConfig], Any] = RerankClient,
    ) -> None:
        self.config = config
        self.control_path = control_path
        self.reload_config = reload_config
        self.embedding_factory = embedding_factory
        self.rerank_factory = rerank_factory
        self.last_reload_error: str | None = None
        self.last_index_error: str | None = None
        self.last_gc_error: str | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="retrieval")
        self._state = asyncio.Lock()
        self._writer = asyncio.Lock()
        self._pins: dict[str, int] = {}
        self._building: set[str] = set()
        self._users: dict[KnowledgeStore, int] = {}
        self._retired: list[KnowledgeStore] = []
        self._store: KnowledgeStore | None = None
        self._watcher: asyncio.Task[None] | None = None
        self._gc: asyncio.Task[None] | None = None
        self._idle = asyncio.Event()
        self._idle.set()
        self._control_lock: Any = None

    async def run(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, partial(function, *args, **kwargs)
        )

    async def _finish(self, function: Callable[..., Any], *args: Any) -> Any:
        """提交/回收不留下后台写入；取消时等本次本地事务完成再传播。"""
        task = asyncio.create_task(self.run(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _open_store(
        self, config: RetrievalConfig, shared_with: KnowledgeStore | None = None
    ) -> KnowledgeStore:
        """线程内构造不可中断；取消时接管并关闭结果，避免遗失锁和子进程。"""
        task = asyncio.create_task(self.run(KnowledgeStore, config, shared_with))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            store = await task
            await self._finish(store.close)
            raise

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _read_control(self) -> dict[str, Any]:
        import fcntl

        self.control_path.parent.mkdir(parents=True, exist_ok=True)
        self._control_lock = self.control_path.with_suffix(".lock").open("a+b")
        try:
            fcntl.flock(self._control_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._control_lock.close()
            raise RetrievalServiceError("检索配置已有运行进程，请关闭后使用独立管理 CLI") from None
        if self.control_path.exists():
            return json.loads(self.control_path.read_text(encoding="utf-8"))
        return {}

    def _save_control(self, store: KnowledgeStore, retiring: KnowledgeStore | None = None) -> None:
        data = {
            "storage": {k: getattr(store.config, k) for k in _STORAGE},
            "last_index_error": self.last_index_error,
            "retired": [
                {
                    **{k: getattr(s.config, k) for k in _STORAGE},
                    "collection_ids": sorted(s.cleanup_ids),
                }
                for s in [*self._retired, *([retiring] if retiring else [])]
            ],
        }
        temp = self.control_path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(self.control_path)

    async def open(self) -> None:
        if self._store is not None:
            return
        try:
            state = await self._finish(self._read_control)
            self.last_index_error = state.get("last_index_error")
            config = self.config.model_copy(update=state.get("storage", {}))
            self._store = await self._open_store(config)
            for paths in state.get("retired", []):
                old_config = config.model_copy(update={k: paths[k] for k in _STORAGE})
                retired = await self._open_store(old_config, self._store)
                retired.cleanup_ids = set(paths.get("collection_ids", []))
                self._retired.append(retired)
            # 先处理迁移旧库，再清当前 retired 代，保留所有可精确识别的旧 collection。
            for retired in list(self._retired):
                await self.run(retired.cleanup_migrated, self._store)
                await self.run(retired.close)
                self._retired.remove(retired)
            assert self._store is not None
            await self.run(self._store.collect, set())
            await self.run(self._save_control, self._store)
            if self.reload_config is not None:
                self._watcher = asyncio.create_task(self._watch())
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watcher
            self._watcher = None
        # 应用关闭等待已取得快照的请求排空；管理方先停止接受新请求。
        await self._idle.wait()
        if self._gc is not None:
            await self._gc
        for store in [*self._retired, *([self._store] if self._store else [])]:
            await self.run(store.close)
        self._retired.clear()
        self._store = None
        if self._control_lock is not None:
            self._control_lock.close()
            self._control_lock = None
        self._executor.shutdown(wait=True, cancel_futures=True)

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(1)
            await self.refresh()

    async def refresh(self) -> None:
        if self.reload_config is None:
            return
        try:
            candidate = await self.run(self.reload_config)
            if candidate == self.config:
                self.last_reload_error = None
                return
            async with self._state:
                assert self._store is not None
                for workspace in await self.run(self._store.workspaces):
                    corpus = await self.run(self._store.active, workspace)
                    assert corpus is not None
                    with_active_identity(candidate, corpus.generation.build_config["cold"])
                # 工厂初始化失败时不切换整份配置；不发任何模型请求。
                clients: list[Any] = []
                try:
                    clients.append(self.embedding_factory(candidate.embedding))
                    clients.append(self.rerank_factory(candidate.rerank))
                finally:
                    for client in clients:
                        await client.aclose()
                self.config = candidate
                self.last_reload_error = None
        except Exception as exc:
            self.last_reload_error = f"{type(exc).__name__}: {exc}"

    @asynccontextmanager
    async def snapshot(self, *, rebuild: bool = False) -> AsyncIterator[RequestSnapshot]:
        await self.refresh()
        async with self._state:
            assert self._store is not None
            store = self._store
            corpus = await self.run(store.active, KNOWLEDGE_INDEX)
            config = self.config
            if corpus and not rebuild:
                config = with_active_identity(config, corpus.generation.build_config["cold"])
            if not rebuild:
                config = config.model_copy(update={k: getattr(store.config, k) for k in _STORAGE})
            embedder = self.embedding_factory(config.embedding)
            try:
                ranker = self.rerank_factory(config.rerank)
            except BaseException:
                await embedder.aclose()
                raise
            identity = corpus.generation.generation_id if corpus else None
            if identity:
                self._pins[identity] = self._pins.get(identity, 0) + 1
            self._users[store] = self._users.get(store, 0) + 1
            self._idle.clear()
            request = RequestSnapshot(
                config, revision(config.model_dump(mode="json")), store, corpus, embedder, ranker
            )
        try:
            yield request
        finally:
            try:
                try:
                    await embedder.aclose()
                finally:
                    await ranker.aclose()
            finally:
                # 客户端关闭失败或取消也必须释放快照，否则应用关闭会永久等候。
                self._users[store] -= 1
                if identity:
                    self._pins[identity] -= 1
                if not any(self._users.values()):
                    self._idle.set()
                if self._gc is None or self._gc.done():
                    self._gc = asyncio.create_task(self._collect_safely())

    async def _collect_safely(self) -> None:
        try:
            await self._collect()
            self.last_gc_error = None
        except Exception as exc:
            self.last_gc_error = f"{type(exc).__name__}: {exc}"

    async def _collect(self) -> None:
        pinned = {k for k, count in self._pins.items() if count} | self._building
        for store in list(self._retired):
            if not self._users.get(store):
                assert self._store is not None
                await self._finish(store.cleanup_migrated, self._store)
                await self._finish(store.close)
                self._retired.remove(store)
                await self._finish(self._save_control, self._store)
        if self._store is not None:
            await self._finish(self._store.collect, pinned)

    async def status(self) -> dict[str, Any]:
        async with self.snapshot() as request:
            status = await self.run(request.store.status, KNOWLEDGE_INDEX)
            active = cold_values(request.config)
            status.update(
                {
                    "active_config_revision": request.config_revision,
                    "pending_rebuild": pending_fields(self.config, active),
                    "last_reload_error": self.last_reload_error,
                    "last_index_error": self.last_index_error,
                    "last_gc_error": self.last_gc_error,
                    "knowledge_enabled": request.config.knowledge_enabled,
                    "sources": [s.model_dump(mode="json") for s in request.config.sources],
                    "storage": {k: getattr(request.store.config, k) for k in _STORAGE},
                }
            )
            return status

    async def index(
        self,
        source_id: str | None = None,
        *,
        prune: bool = False,
        rebuild: bool = False,
        delete_id: str | None = None,
    ) -> dict[str, Any]:
        async with self._writer, self.snapshot(rebuild=rebuild) as request:
            config, old_store = request.config, request.store
            sources = [s for s in config.sources if rebuild or s.source_id == source_id]
            if delete_id is None and not sources:
                raise ValueError("来源未配置")
            target = old_store
            building_id: str | None = None
            committed = False
            migration = any(getattr(config, k) != getattr(old_store.config, k) for k in _STORAGE)
            try:
                async with asyncio.timeout(config.index_timeout_sec):
                    scans = (
                        [await self.run(scan_source, s, config) for s in sources]
                        if delete_id is None
                        else []
                    )
                    prepared = prepare(
                        request.corpus,
                        scans,
                        config,
                        source_id,
                        prune=prune,
                        rebuild=rebuild,
                        delete_id=delete_id,
                    )
                    if (
                        not rebuild
                        and request.corpus
                        and prepared.documents == request.corpus.documents
                        and prepared.chunks == request.corpus.chunks
                    ):
                        if self.last_index_error is not None:
                            self.last_index_error = None
                            await self._finish(self._save_control, old_store)
                        return {
                            "ok": True,
                            **prepared.counts,
                            "embedded": 0,
                            "index_generation": request.corpus.generation.generation_id,
                        }
                    fingerprint = model_fingerprint(config)
                    cached = await self.run(
                        old_store.cached, fingerprint, [c.text_hash for c in prepared.chunks]
                    )
                    dimension = (
                        request.corpus.generation.dimension
                        if request.corpus and request.corpus.generation.fingerprint == fingerprint
                        else 0
                    )
                    corpus, vectors, embedded = await build(
                        prepared, config, request.embedding, cached, dimension
                    )
                    building_id = corpus.generation.generation_id
                    self._building.add(building_id)
                    # 在发布前也构建关键词索引，避免发布一代只有向量可用的资料。
                    from tools.retrieval.search import KeywordIndex

                    await self.run(KeywordIndex, corpus.chunks)
                    if migration:
                        old_store.cleanup_ids = await self.run(old_store.generation_ids)
                        target = await self._open_store(config, old_store)
                        if config.sqlite_path != old_store.config.sqlite_path:
                            known = await self.run(target.generation_ids)
                            ours = await self.run(old_store.generation_ids)
                            if known - ours:
                                raise RetrievalServiceError(
                                    "迁移目标包含其他索引代，请选择独立空目录"
                                )
                        for other in await self.run(old_store.workspaces):
                            previous = await self.run(old_store.active, other)
                            assert previous is not None
                            data = json.loads(json.dumps(previous.generation.build_config))
                            for key in _STORAGE:
                                data["cold"][key] = getattr(config, key)
                                data["config"][key] = getattr(config, key)
                            copied = replace(
                                previous,
                                generation=replace(previous.generation, build_config=data),
                            )
                            cache = await self.run(
                                old_store.cached,
                                copied.generation.fingerprint,
                                [c.text_hash for c in copied.chunks],
                            )
                            await self._finish(target.stage, copied, cache)
                            await self._finish(target.publish, copied.generation)
                    await self._finish(target.stage, corpus, vectors)
                # 重建结束重新读取最新热字段；构建始终使用启动时固定的冷配置。
                await self.refresh()

                async def commit() -> None:
                    nonlocal committed
                    async with self._state:
                        if migration:
                            # 目标已含所有旧活动代。先切控制指针再发布：任何崩溃点
                            # 至少能读取旧活动快照，不会指向缺 collection 的新代。
                            await self.run(self._save_control, target, old_store)
                        try:
                            await self.run(target.publish, corpus.generation)
                        except BaseException:
                            if migration:
                                await self.run(self._save_control, old_store)
                            raise
                        self.last_index_error = None
                        if migration:
                            self._store = target
                            self._retired.append(old_store)
                        committed = True
                        await self.run(self._save_control, target)

                publishing = asyncio.create_task(commit())
                try:
                    await asyncio.shield(publishing)
                except asyncio.CancelledError:
                    await publishing
                    raise
                return {
                    "ok": True,
                    **prepared.counts,
                    "embedded": embedded,
                    "reused_vectors": len(vectors) - embedded,
                    "index_generation": corpus.generation.generation_id,
                    "documents": len(corpus.documents),
                    "chunks": len(corpus.chunks),
                }
            except BaseException as exc:
                self.last_index_error = f"{type(exc).__name__}: {exc}"
                if target is not old_store and target is not self._store:
                    await self._finish(target.close)
                if not committed:
                    await self._finish(self._save_control, old_store)
                raise
            finally:
                if building_id is not None:
                    self._building.discard(building_id)

    async def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        source_id: str | None = None,
        service: str | None = None,
        tags: tuple[str, ...] = (),
        source_ids: tuple[str, ...] | None = None,
    ) -> Any:
        from tools.retrieval.search import search

        async with self.snapshot() as request:
            return await search(
                request, self.run, query, top_k, source_id, service, tags, source_ids
            )
