"""Embedding / Rerank 内部接口；不注册 Agent 工具，不包含知识库或检索编排。"""

from tools.retrieval.adapters import RankedItem
from tools.retrieval.client import EmbeddingClient, RerankClient
from tools.retrieval.errors import (
    RetrievalAuthenticationError,
    RetrievalConfigurationError,
    RetrievalError,
    RetrievalInvalidResponseError,
    RetrievalNotConfiguredError,
    RetrievalRateLimitError,
    RetrievalServiceError,
    RetrievalTimeoutError,
)

__all__ = [
    "EmbeddingClient",
    "RankedItem",
    "RerankClient",
    "RetrievalAuthenticationError",
    "RetrievalConfigurationError",
    "RetrievalError",
    "RetrievalInvalidResponseError",
    "RetrievalNotConfiguredError",
    "RetrievalRateLimitError",
    "RetrievalServiceError",
    "RetrievalTimeoutError",
]
