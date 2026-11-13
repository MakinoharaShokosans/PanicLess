"""同一索引快照上的 BM25/向量召回、RRF、有限重排与查询总预算。"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from tools.retrieval.errors import RetrievalNotConfiguredError, RetrievalServiceError
from tools.retrieval.indexing import validate_vectors
from tools.retrieval.records import Chunk, Hit, SearchResult

if TYPE_CHECKING:
    from tools.retrieval.service import RequestSnapshot

_TOKEN = re.compile(r"[a-zA-Z0-9_./:+-]+|[\u3400-\u9fff]+")


class KeywordIndex:
    def __init__(self, chunks: tuple[Chunk, ...]) -> None:
        import jieba
        from rank_bm25 import BM25Okapi

        self.chunks = chunks
        self.tokenizer = jieba.Tokenizer()
        self.tokenizer.set_dictionary(jieba.get_dict_file().name)
        self.tokens = [self.tokenize(c.text) for c in chunks]
        self.index = BM25Okapi(self.tokens) if any(self.tokens) else None

    def tokenize(self, text: str) -> list[str]:
        tokens = []
        for part in _TOKEN.findall(text.lower()):
            if re.search(r"[\u3400-\u9fff]", part):
                tokens.extend(t for t in self.tokenizer.cut(part, HMM=False) if t.strip())
            else:
                tokens.append(part)
        return tokens

    def query(self, text: str, limit: int) -> list[str]:
        query = self.tokenize(text)
        if self.index is None or not query:
            return []
        scores = self.index.get_scores(query)
        words = set(query)
        # 小语料 BM25 可能为零或负分；用真实词交集判断是否匹配，而非伪造零分候选。
        matches = [
            (self.chunks[i].chunk_id, float(scores[i]))
            for i, tokens in enumerate(self.tokens)
            if words.intersection(tokens)
        ]
        return [key for key, _ in sorted(matches, key=lambda pair: (-pair[1], pair[0]))[:limit]]


async def search(
    request: RequestSnapshot,
    run: Callable[..., Any],
    query: str,
    top_k: int | None,
    source_id: str | None,
    service: str | None,
    tags: tuple[str, ...],
    source_ids: tuple[str, ...] | None = None,
) -> SearchResult:
    config, corpus = request.config, request.corpus
    started = time.monotonic()
    deadline = started + config.search_timeout_sec
    if not config.knowledge_enabled:
        raise RetrievalNotConfiguredError("知识检索已关闭")
    if not query.strip() or len(query) > config.max_query_chars:
        raise ValueError("query 为空或超过 max_query_chars")
    top_k = config.default_top_k if top_k is None else top_k
    if type(top_k) is not int or not 1 <= top_k <= config.max_top_k:
        raise ValueError("top_k 超出当前配置范围")
    if source_ids is not None:
        if not isinstance(source_ids, tuple) or any(
            not isinstance(s, str) or not s.strip() for s in source_ids
        ):
            raise ValueError("source_ids 必须是非空字符串的元组")
        if not set(source_ids).issubset({s.source_id for s in config.sources}):
            raise ValueError("source_ids 包含未配置的来源")
    if corpus is None:
        return SearchResult(query, request.config_revision, None, "not_indexed")
    sources = {
        s.source_id: s
        for s in config.sources
        if (source_id is None or s.source_id == source_id)
        and (source_ids is None or s.source_id in source_ids)
        and (service is None or s.service == service)
        and set(tags).issubset(s.tags)
    }
    documents = {
        d.doc_id: d
        for d in corpus.documents
        if d.source_id in sources and d.source_root == sources[d.source_id].root_path
    }
    chunks = tuple(c for c in corpus.chunks if c.doc_id in documents)
    by_id = {c.chunk_id: c for c in chunks}
    result = SearchResult(query, request.config_revision, corpus.generation.generation_id, "hybrid")
    if not chunks:
        return result

    async def keyword() -> list[str]:
        def recall() -> list[str]:
            return KeywordIndex(chunks).query(query, config.bm25_candidates)

        return await run(recall)

    async def vector() -> list[tuple[str, float]]:
        vectors = await request.embedding.embed([query])
        validate_vectors(vectors, 1, corpus.generation.dimension)
        names = sorted({d.source_id for d in documents.values()})
        return await run(
            request.store.vector_query,
            corpus,
            vectors[0],
            names,
            min(config.vector_candidates, len(chunks)),
        )

    tasks = {"bm25": asyncio.create_task(keyword()), "vector": asyncio.create_task(vector())}
    available: dict[str, Any] = {}
    try:
        _done, pending = await asyncio.wait(
            tasks.values(), timeout=max(0, deadline - time.monotonic())
        )
        for name, task in tasks.items():
            if task in pending:
                task.cancel()
                result.degraded_reasons.append(f"{name}_timeout")
            else:
                try:
                    available[name] = task.result()
                except Exception as exc:
                    result.degraded_reasons.append(f"{name}:{type(exc).__name__}")
    finally:
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
    if not available:
        raise RetrievalServiceError("全部召回通道不可用：" + ", ".join(result.degraded_reasons))
    result.retrieval_mode = "hybrid" if len(available) == 2 else next(iter(available))
    scores: dict[str, Hit] = {}
    for name, values in available.items():
        for rank, value in enumerate(values, 1):
            identity = value[0] if name == "vector" else value
            if identity not in by_id:
                continue
            chunk = by_id[identity]
            hit = scores.setdefault(identity, Hit(chunk, documents[chunk.doc_id], 0))
            hit.rrf_score += 1 / (config.rrf_k + rank)
            if name == "vector":
                hit.vector_rank, hit.vector_distance = rank, value[1]
            else:
                hit.bm25_rank = rank
    candidates = sorted(scores.values(), key=lambda hit: (-hit.rrf_score, hit.chunk.chunk_id))
    candidates = candidates[: config.rerank_candidates]
    if not config.rerank.enabled:
        result.degraded_reasons.append("rerank_disabled")
    elif candidates:
        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                ranked = await request.rerank.rerank(
                    query, [h.chunk.text for h in candidates], len(candidates)
                )
            if len(ranked) != len(candidates) or {r.index for r in ranked} != set(
                range(len(candidates))
            ):
                raise ValueError("rerank candidate mapping invalid")
            ordered = []
            for item in ranked:
                hit = candidates[item.index]
                hit.rerank_score = item.score
                ordered.append(hit)
            candidates = ordered
        except Exception as exc:
            result.degraded_reasons.append(f"rerank:{type(exc).__name__}")
    result.hits = candidates[:top_k]
    from tools.retrieval.expansion import expand_context

    expansion = config.context_expansion
    if time.monotonic() >= deadline:
        result.degraded_reasons.append("context_budget_exhausted")
        expansion = expansion.model_copy(update={"enabled": False})
    # 仅处理 top_k 及有限邻域；没有网络/磁盘或再次模型调用。
    result.context_blocks, result.context_truncated = expand_context(result.hits, chunks, expansion)
    result.duration_ms = (time.monotonic() - started) * 1000
    return result
