"""首版显式协议适配器：请求编码与响应校验，不包含 HTTP 或检索编排。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tools.retrieval.errors import RetrievalInvalidResponseError

FiniteNumber = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Index = Annotated[int, Field(strict=True, ge=0)]


@dataclass(frozen=True, slots=True)
class RankedItem:
    index: int
    score: float


class _Reply(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class _Embedding(_Reply):
    index: Index
    embedding: list[FiniteNumber] = Field(min_length=1)


class _Embeddings(_Reply):
    data: list[_Embedding]


class _Rank(_Reply):
    index: Index
    relevance_score: FiniteNumber


class _Ranking(_Reply):
    results: list[_Rank]


class OpenAIEmbeddingsAdapter:
    @staticmethod
    def request(model: str, texts: list[str]) -> dict[str, object]:
        return {"model": model, "input": texts}

    @staticmethod
    def parse(body: object, count: int) -> list[list[float]]:
        try:
            rows = _Embeddings.model_validate(body).data
        except ValidationError:
            raise RetrievalInvalidResponseError("Embedding 响应格式错误") from None
        indexes = {row.index for row in rows}
        if len(rows) != count or indexes != set(range(count)):
            raise RetrievalInvalidResponseError("Embedding 响应下标缺失、重复或越界")
        if len({len(row.embedding) for row in rows}) != 1:
            raise RetrievalInvalidResponseError("Embedding 响应向量维度不一致")
        return [row.embedding for row in sorted(rows, key=lambda row: row.index)]


class IndexedRerankAdapter:
    @staticmethod
    def request(model: str, query: str, documents: list[str], top_n: int) -> dict[str, object]:
        return {"model": model, "query": query, "documents": documents, "top_n": top_n}

    @staticmethod
    def parse(body: object, count: int, top_n: int) -> list[RankedItem]:
        try:
            rows = _Ranking.model_validate(body).results
        except ValidationError:
            raise RetrievalInvalidResponseError("Rerank 响应格式错误") from None
        indexes = {row.index for row in rows}
        if len(rows) < top_n or len(indexes) != len(rows) or any(i >= count for i in indexes):
            raise RetrievalInvalidResponseError("Rerank 响应数量不足、下标重复或越界")
        ordered = sorted(rows, key=lambda row: (-row.relevance_score, row.index))
        return [RankedItem(row.index, row.relevance_score) for row in ordered[:top_n]]
