"""独立异步 HTTP 模型客户端：显式地址、有限超时、无重试、取消透传。"""

from __future__ import annotations

import asyncio
import os
from typing import Self

import httpx

from baseagent_config.models import EmbeddingConfig, RerankConfig, RetrievalModelConfig
from tools.retrieval.adapters import IndexedRerankAdapter, OpenAIEmbeddingsAdapter, RankedItem
from tools.retrieval.errors import (
    RetrievalAuthenticationError,
    RetrievalConfigurationError,
    RetrievalInvalidResponseError,
    RetrievalNotConfiguredError,
    RetrievalRateLimitError,
    RetrievalServiceError,
    RetrievalTimeoutError,
)


class _ModelClient:
    def __init__(
        self,
        config: RetrievalModelConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        http, self._http = self._http, None
        if http is not None:
            await http.aclose()

    async def _post(self, payload: dict[str, object], protocol: str) -> object:
        config = self._config
        if not config.enabled:
            raise RetrievalNotConfiguredError("模型服务未配置或已禁用")
        if config.protocol != protocol:
            raise RetrievalConfigurationError("模型服务协议与客户端不匹配")
        headers: dict[str, str] = {}
        if config.api_key_env:
            key = os.environ.get(config.api_key_env, "").strip()
            if not key:
                raise RetrievalConfigurationError("模型服务鉴权环境变量缺失")
            headers["Authorization"] = f"Bearer {key}"
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=config.timeout_sec,
                transport=self._transport,
                follow_redirects=False,
                trust_env=False,
            )
        http = self._http
        try:
            # httpx 的阶段超时之外再限制完整请求，避免慢速流无限延长总等待。
            async with asyncio.timeout(config.timeout_sec):
                response = await http.post(config.endpoint_url, json=payload, headers=headers)
        except (TimeoutError, httpx.TimeoutException):
            raise RetrievalTimeoutError("模型服务请求超时") from None
        except httpx.InvalidURL:
            raise RetrievalConfigurationError("模型服务请求地址无效") from None
        except httpx.HTTPError:
            raise RetrievalServiceError("模型服务请求失败") from None
        if response.status_code in {401, 403}:
            raise RetrievalAuthenticationError("模型服务鉴权失败")
        if response.status_code == 429:
            raise RetrievalRateLimitError("模型服务限流")
        if not response.is_success:
            raise RetrievalServiceError(f"模型服务返回非成功状态 {response.status_code}")
        try:
            return response.json()
        except (ValueError, RecursionError):
            raise RetrievalInvalidResponseError("模型服务响应不是有效 JSON") from None


class EmbeddingClient(_ModelClient):
    """接受 ``settings.retrieval.embedding``；调用方负责 ``aclose`` 或异步上下文。"""

    def __init__(
        self, config: EmbeddingConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(config, transport=transport)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        body = await self._post(
            OpenAIEmbeddingsAdapter.request(self._config.model, texts), "openai_embeddings"
        )
        return OpenAIEmbeddingsAdapter.parse(body, len(texts))


class RerankClient(_ModelClient):
    """接受 ``settings.retrieval.rerank``；不在失败时伪造分数或隐式降级。"""

    def __init__(
        self, config: RerankConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(config, transport=transport)

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[RankedItem]:
        if not documents:
            return []
        if type(top_n) is not int or top_n <= 0:
            raise ValueError("非空候选的 top_n 必须是正整数")
        limit = min(top_n, len(documents))
        body = await self._post(
            IndexedRerankAdapter.request(self._config.model, query, documents, limit),
            "indexed_rerank",
        )
        return IndexedRerankAdapter.parse(body, len(documents), limit)
