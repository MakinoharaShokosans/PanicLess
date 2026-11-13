"""构建索引代：确定资料集合、批量调用正式模型接口、复用向量。"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass
from typing import Any

from baseagent_config.models import RetrievalConfig
from baseagent_config.retrieval import cold_values, model_fingerprint
from tools.retrieval.chunking import ScanResult
from tools.retrieval.errors import RetrievalInvalidResponseError, RetrievalServiceError
from tools.retrieval.records import KNOWLEDGE_INDEX, Chunk, Corpus, Document, Generation


@dataclass(frozen=True)
class Prepared:
    documents: tuple[Document, ...]
    chunks: tuple[Chunk, ...]
    counts: dict[str, int]


def prepare(
    old: Corpus | None,
    scans: list[ScanResult],
    config: RetrievalConfig,
    source_id: str | None,
    *,
    prune: bool,
    rebuild: bool,
    delete_id: str | None = None,
) -> Prepared:
    errors = [e for scan in scans for e in scan.errors]
    if errors:
        raise RetrievalServiceError("来源扫描失败，旧索引保持不变：" + "; ".join(errors))
    sources = {s.source_id: s for s in config.sources}
    previous = {d.doc_id: d for d in old.documents} if old else {}
    documents = (
        {}
        if rebuild
        else {
            k: d
            for k, d in previous.items()
            if d.source_id in sources and d.source_root == sources[d.source_id].root_path
        }
    )
    chunks = {c.chunk_id: c for c in old.chunks if c.doc_id in documents} if old else {}
    scanned = {d.doc_id: d for scan in scans for d in scan.documents}
    if delete_id is not None:
        if delete_id not in previous:
            raise ValueError("doc_id 不属于知识库活动索引")
        documents.pop(delete_id, None)
    if prune:
        documents = {k: d for k, d in documents.items() if d.source_id != source_id or k in scanned}
    unchanged = 0
    for key, doc in scanned.items():
        prior = previous.get(key)
        if (
            not rebuild
            and prior
            and prior.content_hash == doc.content_hash
            and prior.source_root == doc.source_root
        ):
            documents[key] = prior
            unchanged += 1
        else:
            documents[key] = doc
    chunks = {
        k: c
        for k, c in chunks.items()
        if c.doc_id in documents and c.revision == documents[c.doc_id].revision
    }
    for scan in scans:
        for chunk in scan.chunks:
            chunks[chunk.chunk_id] = chunk
    if rebuild:
        chunks = {c.chunk_id: c for scan in scans for c in scan.chunks}
    if len(chunks) > config.max_chunks:
        raise RetrievalServiceError("知识库片段总量超过 max_chunks；旧索引保持不变")
    return Prepared(
        tuple(sorted(documents.values(), key=lambda d: d.doc_id)),
        tuple(sorted(chunks.values(), key=lambda c: (c.doc_id, c.ordinal))),
        {
            "added": len(set(documents) - set(previous)),
            "updated": sum(
                k in previous and previous[k].content_hash != d.content_hash
                for k, d in scanned.items()
            ),
            "unchanged": unchanged,
            "deleted": len(set(previous) - set(documents)),
        },
    )


def validate_vectors(vectors: list[list[float]], count: int, dimension: int) -> int:
    if len(vectors) != count:
        raise RetrievalInvalidResponseError("入库向量数量与片段数量不符")
    for vector in vectors:
        if not vector or (dimension and len(vector) != dimension):
            raise RetrievalInvalidResponseError("向量为空或维度发生变化，请检查模型修订")
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in vector):
            raise RetrievalInvalidResponseError("入库向量包含非有限数值")
        dimension = len(vector)
    return dimension


async def build(
    prepared: Prepared,
    config: RetrievalConfig,
    embedder: Any,
    cached: dict[str, list[float]],
    dimension: int = 0,
) -> tuple[Corpus, dict[str, list[float]], int]:
    texts = {c.text_hash: c.text for c in prepared.chunks}
    vectors = {h: v for h, v in cached.items() if h in texts}
    dimension = validate_vectors(list(vectors.values()), len(vectors), dimension)
    missing = [h for h in texts if h not in vectors]
    for start in range(0, len(missing), config.embedding_batch_size):
        hashes = missing[start : start + config.embedding_batch_size]
        batch = await embedder.embed([texts[h] for h in hashes])
        dimension = validate_vectors(batch, len(hashes), dimension)
        vectors.update(zip(hashes, batch, strict=True))
    generation = Generation(
        "plrag_" + uuid.uuid4().hex,
        KNOWLEDGE_INDEX,
        model_fingerprint(config),
        dimension,
        {"cold": cold_values(config), "config": config.model_dump(mode="json")},
        time.time(),
    )
    return Corpus(generation, prepared.documents, prepared.chunks), vectors, len(missing)
