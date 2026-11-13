"""应用级检索装配：首次查询才打开本地库，应用退出时关闭。"""

from __future__ import annotations

import asyncio

from baseagent_config import LoadedSettings
from baseagent_config.models import RetrievalConfig
from baseagent_config.retrieval import RetrievalConfigSource
from tools.retrieval.errors import RetrievalNotConfiguredError, RetrievalServiceError
from tools.retrieval.records import SearchResult
from tools.retrieval.service import RetrievalService


class RuntimeRetrieval:
    def __init__(self, loaded: LoadedSettings) -> None:
        self._loaded = loaded
        self._config = loaded.settings.retrieval
        self._source: RetrievalConfigSource | None = None
        self._service: RetrievalService | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self.last_reload_error: str | None = None

    def current_config(self) -> RetrievalConfig:
        return self._service.config if self._service is not None else self._config

    async def refresh(self) -> None:
        if self._service is not None:
            await self._service.refresh()
            self.last_reload_error = self._service.last_reload_error
            return
        try:
            if self._source is None:
                self._source = RetrievalConfigSource(
                    self._loaded.config_path, home=self._loaded.home
                )
            self._config = self._source()
            self.last_reload_error = None
        except Exception as exc:
            # 保留最后有效配置；错误值不进入提示词或回显可能含凭据的 TOML。
            self.last_reload_error = type(exc).__name__

    async def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        source_ids: tuple[str, ...] | None = None,
    ) -> SearchResult:
        async with self._lock:
            if self._closed:
                raise RetrievalServiceError("检索服务已关闭")
            await self.refresh()
            if not self.current_config().knowledge_enabled:
                raise RetrievalNotConfiguredError("知识检索已关闭")
            if self._service is None:
                if self._source is None or self.last_reload_error:
                    raise RetrievalServiceError("检索配置未能加载")
                service = RetrievalService(
                    self._config,
                    control_path=self._source.control_path,
                    reload_config=self._source,
                )
                await service.open()
                self._service = service
            active = self._service
        return await active.search(query, top_k=top_k, source_ids=source_ids)

    async def aclose(self) -> None:
        # 调用方先停止任务 runner，保证没有新的工具调用进来。
        async with self._lock:
            self._closed = True
            if self._service is not None:
                await self._service.aclose()
                self._service = None
