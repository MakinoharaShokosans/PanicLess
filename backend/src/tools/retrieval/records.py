"""知识库的不可变文档修订、字符区间和索引代数据。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 保留已有存储 schema，所有知识统一使用这一索引键，与会话工作区无关。
KNOWLEDGE_INDEX = "global"


@dataclass(frozen=True, slots=True)
class Section:
    section_id: str
    heading_path: tuple[str, ...]
    char_start: int
    char_end: int


@dataclass(frozen=True, slots=True)
class Document:
    doc_id: str
    workspace_id: str
    source_id: str
    relative_path: str
    source_root: str
    title: str
    content_hash: str
    revision: str
    text: str
    sections: tuple[Section, ...]
    updated_at: float


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_id: str
    doc_id: str
    revision: str
    ordinal: int
    section_id: str
    heading_path: tuple[str, ...]
    line_start: int
    line_end: int
    char_start: int
    char_end: int
    text: str
    text_hash: str


@dataclass(frozen=True, slots=True)
class Generation:
    generation_id: str
    workspace_id: str
    fingerprint: str
    dimension: int
    build_config: dict[str, Any]
    created_at: float


@dataclass(frozen=True, slots=True)
class Corpus:
    generation: Generation
    documents: tuple[Document, ...]
    chunks: tuple[Chunk, ...]


@dataclass(slots=True)
class Hit:
    chunk: Chunk
    document: Document
    rrf_score: float
    vector_rank: int | None = None
    bm25_rank: int | None = None
    vector_distance: float | None = None
    rerank_score: float | None = None
    context_block_id: str | None = None


@dataclass(slots=True)
class ContextBlock:
    block_id: str
    doc_id: str
    revision: str
    source_id: str
    relative_path: str
    anchor_chunk_ids: list[str]
    included_chunk_ids: list[str]
    char_start: int
    char_end: int
    line_start: int
    line_end: int
    text: str
    expansion_mode: str
    truncated: bool = False
    expansion_fallback: bool = False


@dataclass(slots=True)
class SearchResult:
    query: str
    config_revision: str
    index_generation: str | None
    retrieval_mode: str
    degraded_reasons: list[str] = field(default_factory=list)
    duration_ms: float = 0
    hits: list[Hit] = field(default_factory=list)
    context_blocks: list[ContextBlock] = field(default_factory=list)
    context_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        out = asdict(self)
        out["hits"] = [
            {
                **asdict(hit.chunk),
                "title": hit.document.title,
                "source_id": hit.document.source_id,
                "relative_path": hit.document.relative_path,
                "rrf_score": hit.rrf_score,
                "vector_rank": hit.vector_rank,
                "bm25_rank": hit.bm25_rank,
                "vector_distance": hit.vector_distance,
                "rerank_score": hit.rerank_score,
                "context_block_id": hit.context_block_id,
            }
            for hit in self.hits
        ]
        return out
